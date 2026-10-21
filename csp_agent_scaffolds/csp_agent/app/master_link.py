"""
app/master_link.py
The DC master link: one upload link for every working CSP (Labels 1-4, not
Inactive), sent to DCs so they can upload documents for their CSPs. The DC
picks or types a KO code and sees that CSP's live status, exactly as on the
CSP's own page; the CSPs' own links keep working as before.

Rules per document (the server enforces them, the page only shows them):
  - valid with more than RENEW_WITHIN_DAYS left (or valid with no expiry):
    locked, no new copy until RENEW_WITHIN_DAYS before it expires;
  - waiting for a person in the Review queue: locked;
  - expiring within RENEW_WITHIN_DAYS, expired, missing, unreadable or
    rejected: may be uploaded.
On upload, besides the usual checks: a file already on record for another
CSP is refused, and so is the same document as the one on file (same issue
date). Every document stored through the link is also listed in the Review
queue ("DC upload ...") for the daily report.

Only the SHA-256 of the token is stored; creating a new link switches the old
one off; a link expires after MASTER_LINK_DAYS.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import ComplianceState, working_clause
from .config import PUBLIC_BASE_URL
from .models import CSP, MasterLink
from .validation import normalize_csp_code

MASTER_LINK_DAYS = 30
RENEW_WITHIN_DAYS = 60
CHANNEL = "DC_MASTER_LINK"
REVIEW_PREFIX = "DC upload"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_url(token: str) -> str:
    return f"{PUBLIC_BASE_URL}/upload?dc={token}"


def create(db: Session, by: str) -> str:
    """A new master link; any older one stops working. Returns the URL."""
    for r in db.query(MasterLink).filter(MasterLink.revoked_at.is_(None)):
        r.revoked_at = _now()
    token = secrets.token_urlsafe(24)
    db.add(MasterLink(token_hash=_hash(token), created_by=by, created_at=_now(),
                      expires_at=_now() + timedelta(days=MASTER_LINK_DAYS)))
    db.flush()
    return link_url(token)


def revoke_all(db: Session) -> int:
    n = 0
    for r in db.query(MasterLink).filter(MasterLink.revoked_at.is_(None)):
        r.revoked_at, n = _now(), n + 1
    return n


def resolve(db: Session, token: str) -> Optional[MasterLink]:
    if not token or len(token) > 100:
        return None
    r = db.query(MasterLink).filter(MasterLink.token_hash == _hash(token)).first()
    if r is None or r.revoked_at is not None or r.expires_at < _now():
        return None
    return r


def status(db: Session) -> dict:
    r = (db.query(MasterLink).filter(MasterLink.revoked_at.is_(None), MasterLink.expires_at > _now())
         .order_by(MasterLink.created_at.desc()).first())
    if r is None:
        return {"active": False}
    return {"active": True, "created_by": r.created_by, "created_at": r.created_at.isoformat() + "Z",
            "expires_at": r.expires_at.isoformat() + "Z"}


def eligible(db: Session):
    """CSPs on the calling sheet whose terminal works (Labels 1-4)."""
    return db.query(CSP).filter(working_clause())


def find(db: Session, code: str) -> Optional[CSP]:
    want = normalize_csp_code((code or "").strip().upper()[:12])
    if not want:
        return None
    return eligible(db).filter((CSP.current_code == want) | (CSP.lookup_code == want)).first()


def locks(state: ComplianceState) -> dict:
    """Per document type: None (may be uploaded) or why it is locked."""
    out = {}
    for t, s in state.docs.items():
        if s.in_review:
            out[t] = {"key": "dc_in_review"}
        elif s.status == "VALID" and (s.days_left is None or s.days_left > RENEW_WITHIN_DAYS):
            out[t] = {"key": "dc_valid", "expiry": s.expiry_date,
                      "renew_from": s.expiry_date - timedelta(days=RENEW_WITHIN_DAYS) if s.expiry_date else None}
        else:
            out[t] = None
    return out


def lock_message(lock: dict) -> dict:
    fmt = lambda d: d.strftime("%d-%m-%Y")
    if lock["key"] == "dc_in_review":
        return {"hi": "यह डॉक्यूमेंट अभी जाँच में है। नतीजा आने तक दोबारा अपलोड न करें।",
                "en": "This document is being checked by our team. Don't upload it again until then."}
    if lock.get("expiry") is None:
        return {"hi": "यह डॉक्यूमेंट पहले से हमारे पास है और इसकी कोई समाप्ति तारीख नहीं है। नया अपलोड करने की ज़रूरत नहीं।",
                "en": "Already on file, with no expiry date. No new copy is needed."}
    return {"hi": f"यह डॉक्यूमेंट पहले से हमारे पास है और {fmt(lock['expiry'])} तक मान्य है। "
                  f"{fmt(lock['renew_from'])} से रिन्यू किया हुआ डॉक्यूमेंट अपलोड कर सकते हैं।",
            "en": f"Already on file and valid till {fmt(lock['expiry'])}. A renewed copy can be uploaded from "
                  f"{fmt(lock['renew_from'])}."}
