"""
Whose document is it? A document filed under one CSP but showing another
CSP's KO code (in its text, or in its file name, e.g. "1A850074 ASHWANI
KUMAR.pdf" sent in one email with other CSPs' agreements) belongs to that
other CSP. Found on 06-11-2026: 43 such documents on 24 CSPs, 13 of them
counted as the wrong CSP's own (1A851437 Tanishka Yadav had Ashwani Kumar's
agreement and 1A850665's PVR), several accepted by a person who was only
asked about the dates.

Used by the Review queue (a red warning; Accept becomes "Move to <CSP>" or
"Reject"), by its Move button, and by scripts/fix_misfiled_documents.py.
A CSP's own old KO codes (csp_code_history) are not "another CSP".
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import (CSP, AgreementEvent, CSPCodeHistory, Document, DocumentStatus, ManualReviewQueue,
                     ReviewStatus)
from .validation import normalize_csp_code

logger = logging.getLogger("app.owner_guard")
KO = re.compile(r"(?<![0-9A-Z])(\d[A-Z]\d{6})(?![0-9])")
MOVED_PREFIX = "Moved from"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def codes_in_filename(name: Optional[str]) -> list[str]:
    """KO codes written in a file name ("1A851608_SHAKUNTALA GUPTA.pdf",
    "CSP Agreement Ajay Kumar KO 1A852555.pdf")."""
    return list(dict.fromkeys(KO.findall((name or "").upper())))


def codes_shown(doc: Document) -> list[str]:
    """KO codes the document shows: read in its text, or in its file name."""
    f = doc.extracted_fields or {}
    read = f.get("csp_codes") or f.get("csp_code") or []
    out = []
    for c in (read if isinstance(read, list) else [read]):
        if isinstance(c, dict):                 # some readers keep {"value": ..., "confidence": ...}
            c = c.get("value")
        if isinstance(c, str) and c.strip():
            out.append(normalize_csp_code(c))
    out += codes_in_filename(doc.original_filename)
    return list(dict.fromkeys(c for c in out if c and KO.fullmatch(c)))


def own_codes(db: Session, csp: CSP) -> set[str]:
    codes = {c for c in (csp.current_code, csp.lookup_code) if c}
    codes |= {normalize_csp_code(c) for (c,) in db.query(CSPCodeHistory.code).filter(CSPCodeHistory.csp_id == csp.id)
              if c}
    return codes


def csp_by_code(db: Session, code: Optional[str]) -> Optional[CSP]:
    if not code:
        return None
    code = normalize_csp_code(code)
    return db.query(CSP).filter((CSP.current_code == code) | (CSP.lookup_code == code)).first()


def other_owners(db: Session, doc: Document) -> list[CSP]:
    """The other CSPs whose KO code the document shows, when it shows none of
    its own CSP's codes (and no person has said it is this CSP's)."""
    if (doc.extracted_fields or {}).get("owner_confirmed"):
        return []
    csp = db.get(CSP, doc.csp_id)
    shown = codes_shown(doc)
    if csp is None or not shown or own_codes(db, csp) & set(shown):
        return []
    found = {}
    for code in shown:
        o = csp_by_code(db, code)
        if o is not None and o.id != csp.id:
            found[o.id] = o
    return list(found.values())


def warning(db: Session, doc: Document) -> Optional[dict]:
    """For the Review queue: who the document shows, if not its CSP."""
    others = other_owners(db, doc)
    if not others:
        return None
    csp = db.get(CSP, doc.csp_id)
    shown = ", ".join(f"{o.current_code} {o.name}" for o in others)
    one = others[0] if len(others) == 1 else None
    return {"text": f"This document shows {shown}, not {csp.current_code} {csp.name}.",
            "move_to": {"id": one.id, "code": one.current_code, "name": one.name} if one else None}


def move_document(db: Session, doc: Document, to: CSP, by: str, why: str) -> str:
    """File a document under the CSP it belongs to. It counts for that CSP
    only after a person accepts it in the Review queue ("Moved from ...").
    If that CSP already has the same file, this copy is rejected instead.
    Returns "MOVED" or "REJECTED". The caller commits."""
    from . import vault
    from .document_service import recompute_current, sync_agreement_row
    old = db.get(CSP, doc.csp_id)
    for q in db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == doc.id,
                                                ManualReviewQueue.status == ReviewStatus.PENDING):
        # The agent filed it under the wrong CSP: that decision was wrong.
        q.status, q.resolved_at = ReviewStatus.REJECTED, _now()
        q.correction_notes = f"by {by}: filed under the wrong CSP, moved to {to.current_code}"[:500]
    fields = dict(doc.extracted_fields or {})
    same = db.query(Document).filter(Document.csp_id == to.id, Document.sha256 == doc.sha256).first()
    if same is not None:
        doc.status, doc.is_current = DocumentStatus.REJECTED, False
        fields["owner_check"] = (f"{why} {to.current_code} {to.name} already has this same file "
                                 f"(document {same.id}): rejected on {old.current_code}.")[:500]
        doc.extracted_fields = fields
        result, affected = "REJECTED", (old,)
    else:
        fields["moved_from"] = old.current_code
        fields.pop("owner_confirmed", None)
        doc.csp_id, doc.agreement_id, doc.is_current = to.id, None, False
        if doc.status not in (DocumentStatus.UNREADABLE, DocumentStatus.REJECTED):
            doc.status = DocumentStatus.NEEDS_REVIEW
            db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
                f"{MOVED_PREFIX} {old.current_code} {old.name}: {why} Check once that it is {to.name}'s document "
                f"and its dates, then accept: it counts for {to.name} after that.")[:500]))
        doc.extracted_fields = fields
        result, affected = "MOVED", (old, to)
    db.flush()
    for c in affected:
        recompute_current(db, c, doc.document_type)
        sync_agreement_row(db, c)
        try:
            vault.place_csp(db, c)
        except OSError:
            logger.exception("owner_move_file_not_placed csp_id=%s", c.id)
    db.add(AgreementEvent(csp_id=old.id, event_type="DOCUMENT_MOVED" if result == "MOVED" else "DOCUMENT_REJECTED_OWNER",
                          source=by[:100], channel="WEB",
                          payload={"document_id": doc.id, "type": doc.document_type, "from": old.current_code,
                                   "to": to.current_code, "why": why[:300]}))
    logger.info("owner_move doc=%s %s -> %s %s by %s", doc.id, old.current_code, to.current_code, result, by)
    return result
