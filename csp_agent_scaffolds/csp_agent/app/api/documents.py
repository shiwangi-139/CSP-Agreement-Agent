"""
app/api/documents.py
Document serving and download router with human-readable filenames.
Ensures compliance officers and auditors receive clean, descriptive filenames
when downloading or previewing files from the dashboard.
"""

import os
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from ..db import get_db, SessionLocal
from ..models import Document, CSP, DocumentStatus, ManualReviewQueue, ReviewStatus
from ..storage import get_human_readable_download_name, read_file
from ..ocr_service import extract_pdf_pages_text as _extract_pdf_text_pymupdf
from ..ai import service as ai_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _process_document(document_id: int, storage_path: str, mime_type: str):
    """Processes document for review queue and safety approval gate."""
    db = SessionLocal()
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        return
    file_bytes = read_file(storage_path) if storage_path else b""
    text = _extract_pdf_text_pymupdf(file_bytes)
    extraction = ai_service.validate_document(file_bytes, text)
    doc.extracted_fields = extraction.model_dump(mode="json") if hasattr(extraction, "model_dump") else extraction
    doc.status = DocumentStatus.NEEDS_APPROVAL

    queue_item = ManualReviewQueue(
        document_id=doc.id,
        reason="AWAITING_RENEWAL_APPROVAL",
        status=ReviewStatus.PENDING
    )
    db.add(queue_item)
    db.commit()




@router.get("/")
def list_documents(csp_code: str = Query(None), db: Session = Depends(get_db)):
    """Lists current documents, optionally filtered by CSP code."""
    query = db.query(Document)
    if csp_code:
        csp = db.query(CSP).filter((CSP.current_code == csp_code) | (CSP.lookup_code == csp_code)).first()
        if not csp:
            return []
        query = query.filter(Document.csp_id == csp.id)

    docs = query.order_by(Document.uploaded_at.desc()).limit(100).all()
    return [
        {
            "id": d.id,
            "csp_id": d.csp_id,
            "document_type": d.document_type,
            "is_current": d.is_current,
            "status": d.status.value if hasattr(d.status, "value") else str(d.status),
            "issue_date": d.issue_date.isoformat() if d.issue_date else None,
            "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None,
            "validity_rule": d.validity_rule_used,
            "confidence": d.overall_confidence,
            "download_url": f"/api/documents/{d.id}/download",
            "preview_url": f"/api/documents/{d.id}/preview"
        }
        for d in docs
    ]


@router.get("/{doc_id}/download")
def download_document(doc_id: int, db: Session = Depends(get_db)):
    """Serves the document as an attachment with a clean, human-readable filename."""
    return _serve_doc(doc_id, db, as_attachment=True)


@router.get("/{doc_id}/preview")
def preview_document(doc_id: int, db: Session = Depends(get_db)):
    """Serves the document inline for browser PDF preview with a clean filename."""
    return _serve_doc(doc_id, db, as_attachment=False)


def _resolve_existing_file_path(doc: Document) -> Optional[str]:
    from ..vault import abs_path, is_inside_vault
    p = abs_path(doc.storage_path)
    if p is not None and p.is_file() and is_inside_vault(p):
        return str(p)
    return None


def _serve_doc(doc_id: int, db: Session, as_attachment: bool = False):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document record not found.")

    resolved_path = _resolve_existing_file_path(doc)
    if not resolved_path:
        raise HTTPException(status_code=404, detail="Document file does not exist in storage.")

    csp = db.query(CSP).filter(CSP.id == doc.csp_id).first()
    csp_code = csp.current_code or csp.lookup_code if csp else "CSP"
    csp_name = csp.name if csp else "Partner"

    year = doc.issue_date.year if doc.issue_date else None
    ext = os.path.splitext(resolved_path)[1].lstrip(".") or "pdf"
    human_name = get_human_readable_download_name(
        csp_code=csp_code,
        csp_name=csp_name,
        doc_type=doc.document_type,
        year=year,
        ext=ext
    )

    disposition_type = "attachment" if as_attachment else "inline"
    headers = {
        "Content-Disposition": f'{disposition_type}; filename="{human_name}"'
    }

    return FileResponse(
        path=resolved_path,
        media_type=doc.mime_type or "application/pdf",
        headers=headers
    )

