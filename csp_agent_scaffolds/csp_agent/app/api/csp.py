"""
app/api/csp.py
APIs for CSP Master Data, Calling Sheet Synchronization, and Discrepancy Reporting.
"""

from datetime import datetime, timezone
import json
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CSP, Agreement, Document, InboundMessage, OutboundMessage, AgreementEvent, AutoCallingSheetEntry
from ..comms.sheets_sync import sync_calling_sheet
from ..reports_xlsx import generate_discrepancy_workbook, generate_auto_calling_sheet_workbook
from ..email_ingest import evaluate_csp_category
from ..calling_sheet_auto import (
    detect_and_auto_make_calling_sheet,
    promote_auto_entry_to_csp,
    promote_all_auto_entries_to_csp
)

router = APIRouter()


@router.get("/")
def list_csps(limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    """Lists CSPs from master database."""
    total = db.query(CSP).count()
    csps = db.query(CSP).offset(offset).limit(limit).all()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": c.id,
                "name": c.name,
                "code": c.current_code or c.lookup_code,
                "phone": c.phone,
                "email": c.email,
                "region": c.region,
                "branch": c.branch,
                "status": c.status,
                "calling_sheet_synced_at": c.calling_sheet_synced_at.isoformat() if c.calling_sheet_synced_at else None
            }
            for c in csps
        ]
    }


@router.post("/sync-sheet")
def trigger_calling_sheet_sync():
    """
    Triggers an immediate, idempotent sync from Google Spreadsheet 'Calling Sheet New'.
    """
    try:
        summary = sync_calling_sheet()
        return {
            "status": "success",
            "message": "Calling Sheet sync completed successfully.",
            "summary": summary
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync Calling Sheet: {str(e)}")


@router.get("/export-discrepancies-xlsx")
def export_discrepancies_xlsx(db: Session = Depends(get_db)):
    """
    Generates and downloads a multi-tab Excel (.xlsx) workbook containing:
    1. Calling Sheet Discrepancies (unmatched senders)
    2. Calling Sheet Compliance Status (all CSPs, agreements, PVR, Category A/B/C/D)
    3. Outreach & Reply Tracking Log
    4. Executive Summary
    """
    try:
        buffer = generate_discrepancy_workbook(db)
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        filename = f"CSP_Calling_Sheet_Compliance_Discrepancies_{today_str}.xlsx"

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate Excel report: {str(e)}")


@router.get("/discrepancies")
def get_discrepancies(db: Session = Depends(get_db)):
    """
    Returns JSON list of inbound emails from senders not found in Calling Sheet New.
    """
    unmatched = db.query(InboundMessage).filter(
        (InboundMessage.status == "IGNORED_NON_CSP") |
        (InboundMessage.status == "NEEDS_REVIEW")
    ).order_by(InboundMessage.received_at.desc()).limit(100).all()

    items = []
    for msg in unmatched:
        meta = {}
        if msg.error_message:
            try:
                meta = json.loads(msg.error_message)
            except Exception:
                pass
        audit = msg.classification_audit or {}
        items.append({
            "id": msg.id,
            "external_message_id": msg.external_message_id,
            "sender": msg.sender,
            "subject": msg.subject,
            "category": msg.email_category.value if hasattr(msg.email_category, 'value') else str(msg.email_category),
            "candidate_ko": audit.get("extracted_ko") or meta.get("csp_code"),
            "received_at": msg.received_at.isoformat() if msg.received_at else None,
            "status": msg.status,
            "reason": meta.get("reason", "Not registered in Calling Sheet master data"),
            "folder": meta.get("folder", "INBOX")
        })

    return {
        "count": len(items),
        "items": items
    }


@router.get("/compliance-matrix")
def get_compliance_matrix(db: Session = Depends(get_db)):
    """
    Returns compliance category breakdown (A, B, C, D) across all registered CSPs.
    """
    csps = db.query(CSP).all()
    all_agreements = {a.csp_id: a for a in db.query(Agreement).filter(Agreement.is_active.is_(True)).all()}
    all_docs = db.query(Document).all()
    docs_by_csp = {}
    for d in all_docs:
        docs_by_csp.setdefault(d.csp_id, []).append(d)

    categories = {
        "CATEGORY_A": [],
        "CATEGORY_B": [],
        "CATEGORY_C": [],
        "CATEGORY_D": []
    }

    for c in csps:
        agr = all_agreements.get(c.id)
        docs = docs_by_csp.get(c.id, [])
        cat = evaluate_csp_category(c, agr, docs)
        item = {
            "id": c.id,
            "name": c.name,
            "code": c.current_code or c.lookup_code,
            "phone": c.phone,
            "email": c.email,
            "region": c.region,
            "agreement_expiry": agr.expiry_date.isoformat() if agr and agr.expiry_date else None,
            "pvr_expiry": agr.police_verification_expiry.isoformat() if agr and agr.police_verification_expiry else None,
            "doc_count": len(docs)
        }
        if cat in categories:
            categories[cat].append(item)

    return {
        "summary": {
            "total_csps": len(csps),
            "category_a_count": len(categories["CATEGORY_A"]),
            "category_b_count": len(categories["CATEGORY_B"]),
            "category_c_count": len(categories["CATEGORY_C"]),
            "category_d_count": len(categories["CATEGORY_D"]),
        },
        "categories": categories
    }


@router.post("/auto-detect-calling-sheet")
def trigger_auto_detect_calling_sheet(db: Session = Depends(get_db)):
    """
    Scans inbound emails and documents, identifies unlisted CSPs missing from
    'Calling Sheet New', and automatically creates structured Calling Sheet records.
    """
    try:
        summary = detect_and_auto_make_calling_sheet(db)
        return {
            "status": "success",
            "message": "Auto-detection and Calling Sheet generation completed.",
            "summary": summary
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to auto-generate calling sheet: {str(e)}")


@router.get("/auto-calling-sheet")
def get_auto_calling_sheet_entries(db: Session = Depends(get_db)):
    """
    Returns all auto-detected Calling Sheet entries with full extracted attributes.
    """
    entries = db.query(AutoCallingSheetEntry).order_by(AutoCallingSheetEntry.detected_at.desc()).all()
    return {
        "count": len(entries),
        "items": [
            {
                "id": e.id,
                "csp_code": e.csp_code,
                "csp_name": e.csp_name,
                "csp_email": e.csp_email,
                "phone": e.phone,
                "state": e.state,
                "branch": e.branch,
                "circle": e.circle,
                "terminal_status": e.terminal_status,
                "request_type": e.request_type,
                "source_message_id": e.source_message_id,
                "source_subject": e.source_subject,
                "detected_at": e.detected_at.isoformat() if e.detected_at else None,
                "is_promoted_to_master": e.is_promoted_to_master,
                "promoted_csp_id": e.promoted_csp_id
            }
            for e in entries
        ]
    }


@router.get("/export-auto-calling-sheet-xlsx")
def export_auto_calling_sheet_xlsx(db: Session = Depends(get_db)):
    """
    Downloads an Excel (.xlsx) workbook formatted exactly as 'Calling Sheet New'
    containing all auto-detected unlisted CSPs, ready for import into Google Sheets.
    """
    try:
        buffer = generate_auto_calling_sheet_workbook(db)
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        filename = f"Auto_Generated_Calling_Sheet_New_{today_str}.xlsx"

        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate auto calling sheet Excel: {str(e)}")


@router.post("/auto-calling-sheet/{entry_id}/promote")
def promote_entry(entry_id: int, db: Session = Depends(get_db)):
    """
    Promotes an auto-detected calling sheet entry to the master CSP roster,
    starting immediate compliance tracking and document verification.
    """
    try:
        csp = promote_auto_entry_to_csp(db, entry_id)
        return {
            "status": "success",
            "message": f"Successfully promoted {csp.name} (Code: {csp.current_code}) to master CSP roster.",
            "csp_id": csp.id,
            "csp_code": csp.current_code
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auto-calling-sheet/promote-all")
def promote_all_entries(db: Session = Depends(get_db)):
    """
    Bulk-promotes all unpromoted auto-detected calling sheet entries into master CSP roster.
    """
    try:
        summary = promote_all_auto_entries_to_csp(db)
        return {
            "status": "success",
            "message": f"Bulk promotion complete. {summary['promoted_count']} entries added to master roster.",
            "summary": summary
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed bulk promotion: {str(e)}")

