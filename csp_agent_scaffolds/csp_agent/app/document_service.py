"""
app/document_service.py
Store one extracted file for one CSP. Used by Gmail ingestion and the
upload portal so both make identical decisions.

  READABLE     -> file in the CSP folder, Document row; becomes current if
                  its issue date is the newest for that type (the older one
                  moves to expired/)
  UNREADABLE   -> file in unreadable/ for audit, Document row with status
                  UNREADABLE; that type counts as MISSING (never reviewed)

Where each file sits and what it is called is decided by app/vault.py.
  NOT_ALLOWED  -> nothing stored (it may be an Aadhaar/PAN copy)
  DUPLICATE    -> same bytes already stored for this CSP; nothing new
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import vault
from .compliance import USABLE, canonical_type
from .models import Agreement, CSP, Document, DocumentStatus, RenewalStatus

logger = logging.getLogger(__name__)


@dataclass
class StoreOutcome:
    decision: str                   # READABLE, UNREADABLE, NOT_ALLOWED, DUPLICATE
    document: Optional[Document] = None
    reason: Optional[str] = None


def _d(v) -> Optional[date]:
    if not v or v == "LIFETIME_NO_EXPIRY":
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _status_for(ex: dict) -> DocumentStatus:
    if ex.get("compliance_status") == "EXPIRED":
        return DocumentStatus.EXPIRED
    # Model-filled fields are kept, but flagged for a quick look.
    if (ex.get("date_source") or "").startswith("MODEL_VISION"):
        return DocumentStatus.NEEDS_APPROVAL
    return DocumentStatus.VALID


def sync_agreement_row(db: Session, csp: CSP) -> None:
    """Keep the legacy agreements table (used by older dashboard code) in
    step with the current documents."""
    current = {canonical_type(d.document_type): d for d in db.query(Document).filter(
        Document.csp_id == csp.id, Document.is_current.is_(True), Document.readability == "READABLE")}
    agr_doc, pvr_doc = current.get("AGREEMENT"), current.get("POLICE_VERIFICATION")
    if agr_doc is None and pvr_doc is None:
        return
    agr = db.query(Agreement).filter(Agreement.csp_id == csp.id, Agreement.is_active.is_(True)).first()
    if agr is None:
        agr = Agreement(csp_id=csp.id, is_active=True, current_csp_code=csp.current_code,
                        renewal_status=RenewalStatus.ACTIVE)
        db.add(agr)
    if agr_doc is not None:
        agr.start_date, agr.expiry_date = agr_doc.issue_date, agr_doc.expiry_date
        agr.pdf_hash, agr.pdf_link = agr_doc.sha256, agr_doc.storage_path
        expired = agr_doc.expiry_date is not None and agr_doc.expiry_date < date.today()
        agr.renewal_status = RenewalStatus.EXPIRED_LOCKED if expired else RenewalStatus.ACTIVE
    if pvr_doc is not None:
        agr.police_verification_expiry = pvr_doc.expiry_date
    db.flush()
    for d in (agr_doc, pvr_doc):
        if d is not None:
            d.agreement_id = agr.id


def store_extracted_document(
    db: Session, csp: CSP, data: bytes, filename: str, mime_type: Optional[str],
    extraction: dict, channel: str, source_message_id: Optional[str] = None,
    source_date: Optional[datetime] = None, sender_on_sheet: Optional[bool] = None,
) -> StoreOutcome:
    decision = extraction.get("readability")
    sha = hashlib.sha256(data).hexdigest()
    if decision == "NOT_ALLOWED":
        return StoreOutcome("NOT_ALLOWED", reason=extraction.get("rejection_reason"))

    dup = db.query(Document).filter(Document.csp_id == csp.id, Document.sha256 == sha).first()
    if dup is not None:
        return StoreOutcome("DUPLICATE", dup, "Same file already on record.")

    doc_type = canonical_type(extraction.get("document_type")) or "UNKNOWN"
    issue, expiry = _d(extraction.get("start_date")), _d(extraction.get("expiry_date"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    path = vault.stage(data, sha, mime_type or "application/pdf")
    fields = {k: extraction.get(k) for k in (
        "start_date", "expiry_date", "explicit_expiry", "date_source", "validity_rule_used",
        "validity_months", "iibf_registration_number", "holder_name", "csp_code", "ocr_method",
        "extraction_method", "model_provider", "page_count", "pages_readable",
        "readability_reason", "rejection_reason")}
    fields["registration_number"] = extraction.get("iibf_registration_number")
    doc = Document(
        csp_id=csp.id, document_type=doc_type, sha256=sha, file_size_bytes=len(data),
        mime_type=mime_type, storage_path=path,
        status=_status_for(extraction) if decision == "READABLE" else DocumentStatus.UNREADABLE,
        extracted_fields=fields, overall_confidence=extraction.get("confidence"),
        extraction_method=extraction.get("extraction_method"), uploaded_at=now,
        is_current=False, upload_channel=channel, original_filename=(filename or "")[:250],
        issue_date=issue, expiry_date=expiry, validity_rule_used=extraction.get("validity_rule_used"),
        has_explicit_3year_clause=bool(extraction.get("has_explicit_3year_clause")),
        iibf_reg_number=extraction.get("iibf_registration_number"),
        field_confidences=extraction.get("field_confidences"),
        holder_name=extraction.get("holder_name"), validity_months=extraction.get("validity_months"),
        date_source=extraction.get("date_source"), readability=decision,
        source_message_id=source_message_id, source_date=source_date, sender_on_sheet=sender_on_sheet,
    )
    db.add(doc)
    db.flush()
    if decision == "READABLE":
        recompute_current(db, csp, doc_type)
        sync_agreement_row(db, csp)
    vault.place_csp(db, csp)
    return StoreOutcome(decision, doc, extraction.get("readability_reason"))


def recompute_current(db: Session, csp: CSP, doc_type: str) -> Optional[Document]:
    """The one rule for which copy of a document type is current: the newest
    usable, readable copy by issue date, then upload time. Every other copy
    of that type gets is_current=False. Call after anything that adds a
    document, changes its dates, or rejects it."""
    t = canonical_type(doc_type)
    same = [d for d in db.query(Document).filter(Document.csp_id == csp.id)
            if canonical_type(d.document_type) == t]
    usable = [d for d in same if d.status in USABLE and d.readability != "UNREADABLE"]
    best = max(usable, key=lambda d: (d.issue_date or date.min, d.uploaded_at or datetime.min, d.id or 0),
               default=None)
    for d in same:
        d.is_current = d is best
    return best
