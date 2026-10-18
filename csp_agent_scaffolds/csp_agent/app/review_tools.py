"""
app/review_tools.py
Re-read a document waiting in the Review queue with the current rules, so
the reviewer sees what the agent reads today (rules improve; a waiting item
keeps the reading it had when it arrived). Nothing is accepted or rejected
here and no message is sent: the person still decides.
"""
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from . import vault
from .ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from .document_service import _d
from .models import Document, ManualReviewQueue


def reread_waiting(db: Session, q: ManualReviewQueue, d: Document) -> dict:
    """Returns {"changed": bool, "before": ..., "after": ..., "note": str}."""
    path = vault.abs_path(d.storage_path) if d.storage_path else None
    if path is None or not path.exists():
        return {"changed": False, "note": "The stored file is missing."}
    typed = _d((d.extracted_fields or {}).get("typed_issue_date"))
    ex = extract_document_fields_deterministic(path.read_bytes(), path.name, allow_llm_fallback=False,
                                               expected_type=d.document_type, typed_issue_date=typed)
    before = {"issue": d.issue_date, "expiry": d.expiry_date, "rule": d.validity_rule_used}
    if ex.get("readability") != "READABLE" or ex.get("document_type") != d.document_type or not ex.get("start_date"):
        return {"changed": False, "before": before,
                "note": "The current rules still can't read the date or validity: please read it on the document."}
    expiry = ex.get("expiry_date")
    after = {"issue": _d(ex["start_date"]),
             "expiry": None if expiry == "LIFETIME_NO_EXPIRY" else _d(expiry), "rule": ex.get("validity_rule_used")}
    changed = after != before
    d.issue_date, d.expiry_date, d.validity_rule_used = after["issue"], after["expiry"], after["rule"]
    d.date_source = ex.get("date_source")
    d.validity_months = ex.get("validity_months")
    d.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause"))
    d.extracted_fields = {**(d.extracted_fields or {}), "start_date": ex.get("start_date"), "expiry_date": expiry,
                          "reread": f"re-read with the current rules on {date.today().isoformat()}"}
    fmt = lambda x: x.strftime("%d-%m-%Y") if x else "no expiry"
    note = ex.get("type_review") or (f"Re-read with the current rules: issued {fmt(after['issue'])}, valid till "
                                     f"{fmt(after['expiry'])} ({after['rule']}). Check it on the document and accept.")
    if typed:
        note += f" The CSP typed issue date {typed:%d-%m-%Y}."
    q.reason = note[:500]
    db.flush()
    return {"changed": changed, "before": before, "after": after, "note": note}


def waiting_items(db: Session, code: Optional[str] = None):
    from .models import CSP, ReviewStatus
    q = (db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
         .join(CSP, CSP.id == Document.csp_id).filter(ManualReviewQueue.status == ReviewStatus.PENDING))
    if code:
        q = q.filter(CSP.current_code == code.strip().upper())
    return [(r, d, c) for r, d, c in q.order_by(ManualReviewQueue.created_at).all()
            if not (r.reason or "").startswith(("Spot check", "Reference check"))]
