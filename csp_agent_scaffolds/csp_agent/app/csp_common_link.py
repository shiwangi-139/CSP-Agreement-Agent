"""
app/csp_common_link.py
The CSP common link: one upload link for every working CSP, carried by the
final notice (FINAL_NOTICE). The page shows nothing about anyone: the CSP
types their KO code and the last 4 digits of their mobile on record, and if
both match they are sent on to their own upload page (their personal link),
which works exactly as before.

Guessing is blocked: MAX_TRIES wrong tries for one KO code, or MAX_IP_TRIES
from one web address (many CSPs share a mobile network's address, so it is
higher), lock it for LOCK_MINUTES. Every try is kept (common_link_tries).

One live link, never expiring: an admin makes a new one (the old one stops)
or switches it off on the dashboard (Form uploads & DC link), as with the DC
master link (app/master_link.py).
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .config import PUBLIC_BASE_URL
from .models import AgreementEvent, CommonLinkTry, CspCommonLink
from .validation import normalize_csp_code

MAX_TRIES = 5
MAX_IP_TRIES = 20
LOCK_MINUTES = 30
CHANNEL = "CSP_COMMON_LINK"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_url(token: str) -> str:
    return f"{PUBLIC_BASE_URL}/upload?c={token}"


def create(db: Session, by: str) -> str:
    """A new common link; any older one stops working. Returns the URL."""
    revoke_all(db)
    token = secrets.token_urlsafe(18)
    db.add(CspCommonLink(token_hash=_hash(token), token=token, created_by=by, created_at=_now()))
    db.flush()
    return link_url(token)


def revoke_all(db: Session) -> int:
    n = 0
    for r in db.query(CspCommonLink).filter(CspCommonLink.revoked_at.is_(None)):
        r.revoked_at, n = _now(), n + 1
    return n


def resolve(db: Session, token: str) -> Optional[CspCommonLink]:
    if not token or len(token) > 100:
        return None
    r = db.query(CspCommonLink).filter(CspCommonLink.token_hash == _hash(token)).first()
    return r if r is not None and r.revoked_at is None else None


def current_url(db: Session) -> Optional[str]:
    r = (db.query(CspCommonLink).filter(CspCommonLink.revoked_at.is_(None))
         .order_by(CspCommonLink.created_at.desc()).first())
    return link_url(r.token) if r is not None and r.token else None


def status(db: Session) -> dict:
    r = (db.query(CspCommonLink).filter(CspCommonLink.revoked_at.is_(None))
         .order_by(CspCommonLink.created_at.desc()).first())
    since = _now() - timedelta(days=1)
    tries = db.query(CommonLinkTry).filter(CommonLinkTry.at >= since)
    locked = {c for (c,) in db.query(CommonLinkTry.code).filter(
        CommonLinkTry.at >= _now() - timedelta(minutes=LOCK_MINUTES), CommonLinkTry.ok.is_(False))
        .group_by(CommonLinkTry.code).having(func.count(CommonLinkTry.id) >= MAX_TRIES)}
    day = {"opened": tries.filter(CommonLinkTry.ok.is_(True)).count(),
           "wrong": tries.filter(CommonLinkTry.ok.is_(False)).count(), "locked_codes": sorted(c for c in locked if c)}
    if r is None:
        return {"active": False, "last_24h": day}
    return {"active": True, "created_by": r.created_by, "created_at": r.created_at.isoformat() + "Z",
            "url": link_url(r.token) if r.token else None, "last_24h": day}


def _locked(db: Session, code: str, ip: str) -> bool:
    since = _now() - timedelta(minutes=LOCK_MINUTES)
    wrong = db.query(CommonLinkTry).filter(CommonLinkTry.at >= since, CommonLinkTry.ok.is_(False))
    return (bool(code) and wrong.filter(CommonLinkTry.code == code).count() >= MAX_TRIES) \
        or wrong.filter(CommonLinkTry.ip == ip).count() >= MAX_IP_TRIES


def _phones(csp) -> set[str]:
    from .comms.outbound import _phone10
    return {p for p in (_phone10(v) for v in (csp.phone, csp.alt_phone, csp.whatsapp_number, csp.preferred_phone)) if p}


def open_page(db: Session, token: str, code: str, last4: str, ip: str) -> dict:
    """One try. {"result": "ok", "url": the CSP's own link} or a result key
    (closed, bad_input, locked, no_mobile, no_match with "tries_left")."""
    from . import master_link
    from .compliance import REQUIRED_TYPES, evaluate
    from .portal_tokens import issue_upload_link
    if resolve(db, token) is None:
        return {"result": "closed"}
    want = normalize_csp_code((code or "").strip().upper()[:12]) or ""
    digits = "".join(ch for ch in (last4 or "") if ch.isdigit())
    if not want or len(digits) != 4:
        return {"result": "bad_input"}
    ip = (ip or "?")[:64]
    if _locked(db, want, ip):
        return {"result": "locked"}
    csp = master_link.find(db, want)
    phones = _phones(csp) if csp is not None else set()
    if csp is not None and not phones:
        return {"result": "no_mobile"}
    ok = any(p.endswith(digits) for p in phones)
    db.add(CommonLinkTry(at=_now(), code=want, ip=ip, ok=ok, csp_id=csp.id if ok else None))
    if not ok:
        db.commit()
        since = _now() - timedelta(minutes=LOCK_MINUTES)
        wrong = db.query(CommonLinkTry).filter(CommonLinkTry.at >= since, CommonLinkTry.ok.is_(False),
                                               CommonLinkTry.code == want).count()
        return {"result": "locked"} if wrong >= MAX_TRIES else {"result": "no_match", "tries_left": MAX_TRIES - wrong}
    url = issue_upload_link(db, csp, evaluate(db, csp).needs_upload or list(REQUIRED_TYPES))
    db.add(AgreementEvent(csp_id=csp.id, event_type="COMMON_LINK_OPEN", source=CHANNEL, channel="WEB"))
    db.commit()
    return {"result": "ok", "url": url}
