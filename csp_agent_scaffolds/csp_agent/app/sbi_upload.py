"""
app/sbi_upload.py
Agent 3 — which of a CSP's documents are ready to upload to the SBI FI
portal, with the exact values to enter, and what has already been uploaded.

One source of truth for two front-ends:
  - the dashboard "SBI Upload" page (app/api/sbi_portal.py), read-only, on
    the server: it shows the worklist and the status, no browser, no
    credentials;
  - the person-run uploader (sbi_upload/upload_document.py) on a workstation:
    a person logs in (captcha + OTP), the script fills the forms with these
    same values and attaches these same files, and records each upload here.

"Current document" means exactly what it means everywhere else in the agent
(app/compliance.evaluate): the accepted, valid, not-in-review copy. Nothing
here logs in or touches SBI; it only reads the agent's own records.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from . import vault
from .compliance import canonical_type, evaluate
from .models import CSP, Document, DocumentStatus, InternalUser, SbiUpload

# kind (what the SBI tabs are called) <-> the agent's document type
DOC_KINDS = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}
KIND_OF_TYPE = {v: k for k, v in DOC_KINDS.items()}
SHORT = {"agreement": "Agreement", "pvr": "PVR", "iibf": "IIBF"}
ACCEPTED = {DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED}
DEFAULT_SIGNATORY = os.getenv("AGREEMENT_SIGNATORY", "Biswajit Chacko")
DEFAULT_MAX_MB = float(os.getenv("SBI_MAX_UPLOAD_MB", "2"))


@dataclass
class DocToUpload:
    kind: str                       # agreement / pvr / iibf
    document_id: int
    sha256: str
    file: Path
    issue_date: date
    expiry_date: Optional[date] = None
    iibf_reg_number: Optional[str] = None


@dataclass
class CspUpload:
    code: str
    name: str
    state: Optional[str]
    csp_id: Optional[int] = None
    docs: list[DocToUpload] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def already_uploaded(db: Session, csp_id: int, document_type: str, sha256: str) -> Optional[SbiUpload]:
    """The most recent upload record for this CSP and document type, if its
    file matches the one on record now (so a renewed copy is NOT 'uploaded')."""
    row = (db.query(SbiUpload)
           .filter(SbiUpload.csp_id == csp_id, SbiUpload.document_type == canonical_type(document_type))
           .order_by(SbiUpload.uploaded_at.desc()).first())
    return row if row is not None and row.sha256 == sha256 and row.status != "RE_UPLOAD_NEEDED" else None


def record_upload(db: Session, csp: CspUpload, doc: DocToUpload, by: str) -> SbiUpload:
    row = SbiUpload(csp_id=csp.csp_id, document_id=doc.document_id, document_type=DOC_KINDS[doc.kind],
                    sha256=doc.sha256, issue_date=doc.issue_date, expiry_date=doc.expiry_date,
                    uploaded_by=(by or "")[:120], uploaded_at=_now(), status="SUBMITTED")
    db.add(row)
    db.commit()
    return row


def _doc_for(db: Session, csp: CSP, kind: str, st, max_mb: float) -> tuple[Optional[DocToUpload], Optional[str]]:
    """A DocToUpload for one CSP and kind, or (None, reason it is skipped)."""
    s = st.docs[DOC_KINDS[kind]]
    d = s.document
    if s.status != "VALID" or getattr(s, "in_review", False) or d is None or d.status not in ACCEPTED:
        return None, f"no accepted valid copy ({getattr(s, 'shown_status', s.status)})"
    if not s.issue_date:
        return None, "no issue date on record"
    path = vault.abs_path(d.storage_path)
    if path is None or not path.exists():
        return None, f"file missing in the vault (document {d.id})"
    if path.stat().st_size > max_mb * 1024 * 1024:
        return None, f"file is {path.stat().st_size / 1048576:.1f} MB, over {max_mb:g} MB"
    reg = (d.iibf_reg_number or "").strip() or None
    if kind == "agreement" and not s.expiry_date:
        return None, "no end date on record"
    if kind == "pvr" and not (csp.state or "").strip():
        return None, "no state on the calling sheet (needed as PVR issuing authority)"
    if kind == "iibf" and not re.fullmatch(r"\d{9}", reg or ""):
        return None, f"registration number '{reg or '-'}' is not 9 digits"
    return DocToUpload(kind=kind, document_id=d.id, sha256=d.sha256, file=path, issue_date=s.issue_date,
                       expiry_date=s.expiry_date, iibf_reg_number=reg), None


def plan(db: Session, codes: Optional[list[str]] = None, kinds: Iterable[str] = tuple(DOC_KINDS),
         pending: bool = False, max_mb: float = DEFAULT_MAX_MB) -> list[CspUpload]:
    """For the uploader: CSPs whose current valid documents can be uploaded
    and have not been uploaded before. `codes`: specific CSPs; else `pending`
    means every active CSP."""
    kinds = [k for k in kinds if k in DOC_KINDS]
    q = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True))
    if codes:
        csps = q.filter(CSP.current_code.in_(codes)).all()
    elif pending:
        csps = q.order_by(CSP.current_code).all()
    else:
        csps = []
    out: list[CspUpload] = []
    for c in csps:
        item = CspUpload(code=c.current_code, name=c.name, state=c.state, csp_id=c.id)
        st = evaluate(db, c)
        for kind in kinds:
            doc, why = _doc_for(db, c, kind, st, max_mb)
            if doc is None:
                item.skipped.append(f"{kind}: {why}")
            elif already_uploaded(db, c.id, DOC_KINDS[kind], doc.sha256):
                item.skipped.append(f"{kind}: this copy was already uploaded")
            else:
                item.docs.append(doc)
        if item.docs or codes:
            out.append(item)
    return out


def dashboard_rows(db: Session, scope=None, max_mb: float = DEFAULT_MAX_MB,
                   signatory: str = DEFAULT_SIGNATORY) -> dict:
    """For the SBI Upload page: per CSP and document, the status, the exact
    values to enter, and the file. Scope: an extra CSP filter (an RM's own)."""
    from .compliance import is_inactive
    q = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True))
    if scope is not None:
        q = q.filter(scope)
    staff = {u.id: u for u in db.query(InternalUser).all()}
    iso = lambda v: v.isoformat() if v else None
    rows, counts = [], {"ready": 0, "uploaded": 0, "waiting": 0, "reupload": 0, "blocked": 0}
    for c in q.all():
        if is_inactive(c):
            continue
        st = evaluate(db, c)
        rm, dc = staff.get(c.rm_id), staff.get(c.dc_id)
        for kind in DOC_KINDS:
            dtype = DOC_KINDS[kind]
            doc, why = _doc_for(db, c, kind, st, max_mb)
            up = (db.query(SbiUpload).filter(SbiUpload.csp_id == c.id, SbiUpload.document_type == dtype)
                  .order_by(SbiUpload.uploaded_at.desc()).first())
            s = st.docs[dtype]
            if doc is not None:
                values = ({"signatory": signatory, "start_date": iso(doc.issue_date), "end_date": iso(doc.expiry_date)}
                          if kind == "agreement" else
                          {"issuing_authority": c.state, "issue_date": iso(doc.issue_date)} if kind == "pvr" else
                          {"registration_number": doc.iibf_reg_number, "issue_date": iso(doc.issue_date)})
                if up is not None and up.sha256 == doc.sha256 and up.status != "RE_UPLOAD_NEEDED":
                    status = "uploaded"
                elif up is not None:
                    status = "reupload"      # a newer copy than the one uploaded
                else:
                    status = "ready"
            else:
                values, status = {}, ("blocked" if "over" in (why or "") else "waiting")
            counts[status] = counts.get(status, 0) + 1
            rows.append({
                "csp": {"id": c.id, "code": c.current_code, "name": c.name},
                "rm": rm.name if rm else "No RM", "rm_id": c.rm_id, "dc": dc.name if dc else "No DC",
                "state": c.state or "", "kind": kind, "doc_label": SHORT[kind], "document_type": dtype,
                "status": status, "values": values, "reason": why,
                "file": doc.file.name if doc is not None else None,
                "issue_date": iso(doc.issue_date) if doc is not None else None,
                "expiry_date": iso(doc.expiry_date) if doc is not None else None,
                "uploaded": None if up is None else {"at": iso(up.uploaded_at), "by": up.uploaded_by,
                                                     "status": up.status},
            })
    return {"counts": counts, "signatory": signatory, "max_mb": max_mb, "rows": rows}


def last_upload(db: Session) -> Optional[dict]:
    r = db.query(SbiUpload).order_by(SbiUpload.uploaded_at.desc()).first()
    return None if r is None else {"at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                                   "by": r.uploaded_by, "code": (db.get(CSP, r.csp_id).current_code
                                                                 if db.get(CSP, r.csp_id) else None)}
