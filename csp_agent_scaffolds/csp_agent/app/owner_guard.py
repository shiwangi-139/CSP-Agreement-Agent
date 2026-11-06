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


# ------------------------------------------------- suggested owner (name + address)
# When no KO code says whose a document is (the same file on several CSPs, or
# no code/name confirmed), the name and the address on it point to a CSP: a
# full name beats a partial one ("Manoj Kumar" is 1A851613 Manoj Kumar more
# than 1A851448 Manoj Kumar Singh), and the district, PIN code or village of
# the CSP's address (calling sheet) on the document support it. Only a
# suggestion with its reasons: police certificates carry the HOME address and
# the sheet the SHOP address, so a person clicks; only a KO code moves a
# document by itself.
ADDRESS_STOP = set("""VILL VILLAGE POST DIST DISTT DISTRICT ROAD MAIN NEAR WARD BLOCK TOLA BAZAR BAZAAR MARKET CHOWK
NAGAR COLONY MOHALLA THANA SHOP HOUSE OPPOSITE OPP BEHIND STATION POLICE FLOOR STREET GALI LANE SECTOR PHASE TEHSIL
TAHSIL MANDAL PANCHAYAT GRAM KHURD KALAN NAYA PURANA INDIA BIHAR UTTAR PRADESH JHARKHAND HARYANA PUNJAB DELHI
RAJASTHAN MAHARASHTRA MADHYA WEST BENGAL ODISHA GUJARAT KARNATAKA CSP KIOSK EKO BANK BRANCH""".split())
PIN = re.compile(r"(?<!\d)\d{6}(?!\d)")


def _upper_words(s: Optional[str]) -> list[str]:
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).split()


def _evidence(c: CSP, text: str, words: set[str]) -> tuple[int, list[str], dict]:
    from .gmail_ingest import _letters, _name_on_document
    from .rm_pending import district_from_address
    score, why, flags = 0, [], {"full_name": False, "district": None}
    name_words = [w for w in dict.fromkeys(_letters(c.name).split()) if len(w) >= 3]
    if name_words and _name_on_document(c.name, _letters(text)):
        score += 3
        flags["full_name"] = True
        why.append(f"full name {c.name} on it")
    elif name_words:
        got = [w for w in name_words if w in words]
        if got:
            score += 1
            why.append(f"only {' '.join(w.title() for w in got)} of the name "
                       f"({' '.join(w.title() for w in name_words if w not in got)} not on it)")
        else:
            why.append("name not on it")
    district = c.district or district_from_address(c.kiosk_location)
    d_words = _letters(district).split() if district else []
    if d_words:
        if all(w in words for w in d_words):
            score += 2
            flags["district"] = True
            why.append(f"district {district.strip().title()} on it")
        else:
            flags["district"] = False
            why.append(f"district {district.strip().title()} not on it")
    address = c.kiosk_location or ""
    if set(PIN.findall(address)) & set(PIN.findall(text)):
        score += 2
        why.append("PIN code of the address on it")
    places = [w for w in dict.fromkeys(_letters(address).split())
              if len(w) >= 4 and w not in ADDRESS_STOP and w not in d_words and w not in name_words]
    hit = [w for w in places if w in words][:2]
    if hit:
        score += len(hit)
        why.append("place " + ", ".join(w.title() for w in hit) + " on it")
    return score, why, flags


def suggest_owner(text: Optional[str], candidates: list[CSP]) -> Optional[dict]:
    """{"best": the CSP the name and address clearly point to (or None),
    "candidates": each CSP with its score and reasons}."""
    seen, cands = set(), []
    for c in candidates:
        if c is not None and c.id not in seen:
            seen.add(c.id)
            cands.append(c)
    if not text or not cands:
        return None
    words = set(_upper_words(text))
    rows = sorted(((*_evidence(c, text, words), c) for c in cands), key=lambda r: -r[0])
    top, why, _, best = rows[0]
    second = rows[1][0] if len(rows) > 1 else 0
    clear = top >= 3 and top - second >= 2 if len(rows) > 1 else top >= 2
    return {"best": {"id": best.id, "code": best.current_code, "name": best.name} if clear else None,
            "candidates": [{"id": c.id, "code": c.current_code, "name": c.name, "score": s, "why": "; ".join(w), **f}
                           for s, w, f, c in rows]}


def decisive_other(sug: Optional[dict], csp_id: int) -> Optional[dict]:
    """Another CSP the document is surely not this CSP's but theirs (decided
    with the team on 06-11-2026): that CSP's FULL NAME and DISTRICT are on it,
    while this CSP's full name is not and its district (known) is different.
    Then it is moved / rejected without a person; anything less stays a
    suggestion (police certificates carry the home address, the sheet the shop)."""
    if not sug or not sug.get("best") or sug["best"]["id"] == csp_id:
        return None
    by_id = {c["id"]: c for c in sug["candidates"]}
    other, this = by_id.get(sug["best"]["id"]), by_id.get(csp_id)
    if not other or not this:
        return None
    if other.get("full_name") and other.get("district") is True \
            and not this.get("full_name") and this.get("district") is False:
        return other
    return None


def suggestion_for_review(db: Session, doc: Document) -> Optional[dict]:
    """The stored suggestion, with what accepting it does now: this CSP's
    (accept), another CSP's that already has the same file (reject here), or
    another CSP's (move)."""
    s = (doc.extracted_fields or {}).get("owner_suggestion")
    if not s:
        return None
    best = s.get("best")
    if best:
        if best["id"] == doc.csp_id:
            best = {**best, "action": "this"}
        else:
            has = db.query(Document.id).filter(Document.csp_id == best["id"], Document.sha256 == doc.sha256).first()
            best = {**best, "action": "reject" if has else "move"}
    return {**s, "best": best}


def holders_of_same_file(db: Session, doc_or_sha, csp_id: Optional[int] = None) -> list[CSP]:
    sha = doc_or_sha if isinstance(doc_or_sha, str) else doc_or_sha.sha256
    q = db.query(CSP).join(Document, Document.csp_id == CSP.id).filter(Document.sha256 == sha,
                                                                       Document.status != DocumentStatus.REJECTED)
    return [c for c in q.distinct().all() if c.id != csp_id]
