"""
app/api/review.py
Human-in-the-loop manual review fallback API for documents requiring verification.
Allows operators to inspect unreadable/low-confidence documents, manually correct
extracted fields, and maintain a complete audit trail in Neon PostgreSQL.
"""
import logging
from datetime import datetime, timezone, date
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session

from pydantic import BaseModel
from ..db import get_db
from ..models import (
    ManualReviewQueue, ReviewStatus, Document, DocumentStatus,
    ExtractionCorrection, Agreement, AgreementEvent, CSP,
    InternalUser, RenewalStatus
)
from ..validation import normalize_csp_code

logger = logging.getLogger(__name__)
router = APIRouter()


class RenewalApproval(BaseModel):
    reviewer_id: int
    notes: str = "Approved"



@router.get("/queue")
def list_review_queue(db: Session = Depends(get_db)):
    """Lists all documents pending manual human review."""
    pending_items = db.query(ManualReviewQueue).filter(
        ManualReviewQueue.status == ReviewStatus.PENDING
    ).order_by(ManualReviewQueue.created_at.desc()).all()

    results = []
    for item in pending_items:
        doc = db.query(Document).filter(Document.id == item.document_id).first()
        if not doc:
            continue
        csp = db.query(CSP).filter(CSP.id == doc.csp_id).first()
        
        extracted = doc.extracted_fields or {}
        results.append({
            "queue_id": item.id,
            "document_id": doc.id,
            "csp_id": doc.csp_id,
            "csp_code": csp.current_code if csp else "UNKNOWN",
            "csp_name": csp.name if csp else "Unknown CSP",
            "document_type": doc.document_type or "UNKNOWN",
            "reason": item.reason,
            "sha256": doc.sha256[:12] if doc.sha256 else "N/A",
            "full_sha256": doc.sha256 or "",
            "overall_confidence": round(doc.overall_confidence * 100, 1) if doc.overall_confidence else 0.0,
            "extracted_fields": extracted,
            "raw_text_snippet": (extracted.get("raw_text_snippet") or extracted.get("ocr_snippet") or "No raw text available")[:400],
            "created_at": item.created_at.isoformat() if item.created_at else None
        })

    return {
        "status": "success",
        "total_pending": len(results),
        "items": results
    }


@router.get("/{queue_id}")
def get_review_item(queue_id: int, db: Session = Depends(get_db)):
    """Get full details of a specific item in the manual review queue."""
    item = db.query(ManualReviewQueue).filter(ManualReviewQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Review queue item not found")

    doc = db.query(Document).filter(Document.id == item.document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Underlying document not found")

    csp = db.query(CSP).filter(CSP.id == doc.csp_id).first()
    return {
        "queue_id": item.id,
        "document_id": doc.id,
        "csp": {
            "id": csp.id if csp else None,
            "code": csp.current_code if csp else "UNKNOWN",
            "name": csp.name if csp else "Unknown CSP",
            "phone": csp.phone if csp else "",
            "email": csp.email if csp else ""
        },
        "document": {
            "type": doc.document_type,
            "status": doc.status.value if hasattr(doc.status, "value") else str(doc.status),
            "sha256": doc.sha256,
            "confidence": doc.overall_confidence,
            "extracted_fields": doc.extracted_fields or {},
            "storage_path": doc.storage_path
        },
        "review_status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "reason": item.reason,
        "created_at": item.created_at.isoformat() if item.created_at else None
    }


@router.post("/{queue_id}/correct")
def submit_manual_correction(queue_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Submits authorized manual human correction for an unresolved document.
    Updates Document status to MANUAL_VERIFIED, recalculates Agreement dates,
    and logs immutable audit records to ExtractionCorrection and AgreementEvent.
    """
    item = db.query(ManualReviewQueue).filter(ManualReviewQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")

    doc = db.query(Document).filter(Document.id == item.document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    csp = db.query(CSP).filter(CSP.id == doc.csp_id).first()

    # 1. Resolve or update CSP if corrected
    new_csp_code = payload.get("csp_code")
    if new_csp_code and csp and (csp.current_code != new_csp_code):
        matched_csp = db.query(CSP).filter(
            (CSP.current_code == new_csp_code) | (CSP.lookup_code == new_csp_code)
        ).first()
        if matched_csp:
            doc.csp_id = matched_csp.id
            csp = matched_csp

    # 2. Extract corrections
    corrections = payload.get("fields", payload)
    doc_type = corrections.get("document_type") or doc.document_type
    issue_date_str = corrections.get("issue_date") or corrections.get("start_date")
    expiry_date_str = corrections.get("expiry_date")
    operator_notes = payload.get("notes", "Manual verification completed via Operations Console")
    operator_name = payload.get("corrected_by_name", "Operations Operator")

    # Record field changes in ExtractionCorrection
    extracted_fields = dict(doc.extracted_fields or {})
    for field_name, new_val in [
        ("document_type", doc_type),
        ("issue_date", issue_date_str),
        ("expiry_date", expiry_date_str),
        ("certificate_number", corrections.get("certificate_number")),
        ("registration_number", corrections.get("registration_number")),
        ("issuing_authority", corrections.get("issuing_authority")),
        ("state", corrections.get("state"))
    ]:
        if new_val:
            old_val = str(extracted_fields.get(field_name, ""))
            if old_val != str(new_val):
                db.add(ExtractionCorrection(
                    document_id=doc.id,
                    field_name=field_name,
                    ai_value=old_val,
                    human_value=str(new_val),
                    ai_confidence=doc.overall_confidence
                ))
            extracted_fields[field_name] = new_val

    # 3. Update Document
    doc.document_type = doc_type
    doc.extracted_fields = extracted_fields
    doc.status = DocumentStatus.MANUAL_VERIFIED
    doc.overall_confidence = 1.0
    doc.extraction_method = "MANUAL_HUMAN_VERIFIED"

    # 4. Update Agreement based on document type
    agr = db.query(Agreement).filter(Agreement.csp_id == doc.csp_id, Agreement.is_active.is_(True)).first()
    if not agr and csp:
        agr = Agreement(
            csp_id=csp.id,
            is_active=True,
            current_csp_code=csp.current_code or csp.lookup_code
        )
        db.add(agr)
        db.flush()

    if agr:
        doc.agreement_id = agr.id
        if doc_type == "AGREEMENT":
            if expiry_date_str and expiry_date_str != "LIFETIME_NO_EXPIRY":
                try: agr.expiry_date = date.fromisoformat(expiry_date_str)
                except Exception: pass
            if issue_date_str:
                try: agr.start_date = date.fromisoformat(issue_date_str)
                except Exception: pass
        elif doc_type == "POLICE_VERIFICATION":
            if expiry_date_str:
                try: agr.police_verification_expiry = date.fromisoformat(expiry_date_str)
                except Exception: pass

    # 5. Close review queue item
    item.status = ReviewStatus.CORRECTED
    item.resolved_at = datetime.now(timezone.utc)
    item.correction_notes = f"{operator_name}: {operator_notes}"

    # 6. Immutable Audit Event
    db.add(AgreementEvent(
        csp_id=csp.id if csp else None,
        agreement_id=agr.id if agr else None,
        event_type="DOCUMENT_MANUAL_VERIFIED",
        source="DASHBOARD_REVIEW_CONSOLE",
        notes=f"Corrected by {operator_name}. Notes: {operator_notes}"
    ))

    db.commit()
    logger.info(f"Manual correction saved for document {doc.id}, queue item {queue_id}")
    return {
        "status": "success",
        "message": f"Document #{doc.id} successfully verified and updated.",
        "document_id": doc.id,
        "status": doc.status.value
    }


@router.post("/{queue_id}/reject")
def reject_review_document(queue_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Marks a document as invalid or rejected by human reviewer."""
    item = db.query(ManualReviewQueue).filter(ManualReviewQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")

    doc = db.query(Document).filter(Document.id == item.document_id).first()
    if doc:
        doc.status = DocumentStatus.REJECTED

    reason = payload.get("reason", "Rejected by human reviewer")
    item.status = ReviewStatus.REJECTED
    item.resolved_at = datetime.now(timezone.utc)
    item.correction_notes = reason

    db.commit()
    return {"status": "success", "message": f"Document #{item.document_id} marked as rejected."}


@router.post("/{queue_id}/approve")
def approve_renewal(queue_id: int, payload: RenewalApproval, db: Session = Depends(get_db)):
    """Authorizes agreement renewal by an authorized role (DC, ADMIN, LHO)."""
    item = db.query(ManualReviewQueue).filter(ManualReviewQueue.id == queue_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Review item not found")

    reviewer = db.query(InternalUser).filter(InternalUser.id == payload.reviewer_id).first()
    if not reviewer or reviewer.role not in ("DC", "ADMIN", "LHO"):
        raise HTTPException(status_code=403, detail="Unauthorized role for renewal approval")

    doc = db.query(Document).filter(Document.id == item.document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.status = DocumentStatus.VALID
    doc.extraction_method = "APPROVED_BY_REVIEWER"

    agr = db.query(Agreement).filter(Agreement.csp_id == doc.csp_id, Agreement.is_active.is_(True)).first()
    if not agr:
        agr = Agreement(csp_id=doc.csp_id, is_active=True)
        db.add(agr)
        db.flush()

    extracted = doc.extracted_fields or {}
    exp_str = None
    if isinstance(extracted, dict):
        if "agreement_expiry_date" in extracted:
            field = extracted["agreement_expiry_date"]
            if isinstance(field, dict) and "value" in field:
                exp_str = field["value"]
            elif isinstance(field, str):
                exp_str = field
        elif "expiry_date" in extracted:
            exp_str = extracted["expiry_date"]

    if exp_str and exp_str != "LIFETIME_NO_EXPIRY":
        try:
            agr.expiry_date = date.fromisoformat(str(exp_str))
        except Exception:
            pass

    agr.renewal_status = RenewalStatus.RENEWED
    doc.agreement_id = agr.id
    item.status = ReviewStatus.APPROVED
    item.resolved_at = datetime.now(timezone.utc)
    item.assigned_to = reviewer.id

    db.add(AgreementEvent(
        csp_id=doc.csp_id,
        agreement_id=agr.id,
        event_type="RENEWAL_APPROVED",
        source="DASHBOARD_REVIEW_CONSOLE",
        notes=f"Approved by {reviewer.name} ({reviewer.role})"
    ))
    db.commit()
    return {"status": "RENEWED", "document_id": doc.id, "agreement_id": agr.id}

