"""
Upload-link tokens for the CSP portal.

A token is a random 32-byte string stored in portal_tokens, not a signed
blob, so there is no secret key to leak or forge with: a link works only
while its row exists, is unexpired and not revoked.

Each CSP has ONE live link. A new message reuses it (adding any newly
requested documents), so a link a CSP already received never dies because
another reminder was drafted. It stays valid PORTAL_TOKEN_DAYS after the
latest message carrying it was sent, and is revoked automatically once every
document it asked for has been accepted.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .config import PUBLIC_BASE_URL, PORTAL_TOKEN_DAYS
from .models import CSP, PortalToken


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _url(jti: str) -> str:
    return f"{PUBLIC_BASE_URL}/upload?token={jti}"


def _live(db: Session, csp_id: int) -> Optional[PortalToken]:
    return (db.query(PortalToken)
            .filter(PortalToken.csp_id == csp_id, PortalToken.revoked_at.is_(None), PortalToken.expires_at > _now())
            .order_by(PortalToken.created_at.desc()).first())


def issue_upload_link(db: Session, csp: CSP, requested_types: list[str], days: int = PORTAL_TOKEN_DAYS) -> str:
    """The CSP's live link (created if there is none), now also asking for
    `requested_types` and valid at least `days` more days."""
    now = _now()
    row = _live(db, csp.id)
    if row is None:
        row = PortalToken(jti=secrets.token_urlsafe(32), csp_id=csp.id, requested_types=list(requested_types),
                          created_at=now, expires_at=now + timedelta(days=days))
        db.add(row)
    else:
        row.requested_types = list(dict.fromkeys([*(row.requested_types or []), *requested_types]))
        row.expires_at = max(row.expires_at, now + timedelta(days=days))
    db.flush()
    return _url(row.jti)


def current_upload_link(db: Session, csp: CSP) -> Optional[str]:
    """The CSP's live link, or None. Never creates one."""
    row = _live(db, csp.id)
    return _url(row.jti) if row else None


def extend_for_sent_message(db: Session, link: Optional[str], days: int = PORTAL_TOKEN_DAYS) -> None:
    """A message carrying `link` was just sent: keep the link valid `days`
    from now, so the CSP always has the full window to use it."""
    if not link or "token=" not in link:
        return
    row = db.get(PortalToken, link.split("token=", 1)[1].split("&")[0])
    if row is not None and row.revoked_at is None:
        row.expires_at = max(row.expires_at, _now() + timedelta(days=days))


def resolve_token(db: Session, token: str) -> Optional[PortalToken]:
    """The live token row, or None if unknown, expired or revoked."""
    if not token or len(token) > 100:
        return None
    row = db.get(PortalToken, token)
    if row is None or row.revoked_at is not None or row.expires_at < _now():
        return None
    return row


def revoke_if_complete(db: Session, row: PortalToken, still_needed: list[str]) -> None:
    requested = set(row.requested_types or [])
    if requested and not (requested & set(still_needed)):
        row.revoked_at, row.revoke_reason = _now(), "ALL_REQUESTED_DOCUMENTS_ACCEPTED"
