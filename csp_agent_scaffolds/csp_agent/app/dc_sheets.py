"""
app/dc_sheets.py
The DCs' live Google Sheets. A Google Apps Script in the admin's own Google
account (scripts/google_sheets/dc_live_sheets.gs) asks the server every 2
hours for each DC's table and rewrites that DC's sheet; the sheets' published
links then show it. The server gives a table only with the current sheet key
(made on the dashboard); the script holds the key, the server keeps it.
"""
import hmac
import secrets
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import DcSheetKey, InternalUser


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current(db: Session) -> Optional[DcSheetKey]:
    return (db.query(DcSheetKey).filter(DcSheetKey.revoked_at.is_(None))
            .order_by(DcSheetKey.created_at.desc()).first())


def create(db: Session, by: str) -> str:
    """A new key; the old one stops working (the script needs the new one)."""
    for r in db.query(DcSheetKey).filter(DcSheetKey.revoked_at.is_(None)):
        r.revoked_at = _now()
    token = secrets.token_urlsafe(32)
    db.add(DcSheetKey(token=token, created_by=by, created_at=_now()))
    db.flush()
    return token


def valid(db: Session, key: str) -> bool:
    row = current(db)
    return bool(row and key) and hmac.compare_digest(row.token.encode(), key.strip().encode())


def find_dc(db: Session, name: str) -> Optional[InternalUser]:
    want = (name or "").strip().lower()
    if not want:
        return None
    if want in ("no dc", "no-dc", "nodc"):
        from .reports import NO_DC
        return NO_DC
    return next((u for u in db.query(InternalUser).filter(InternalUser.role == "DC")
                 if (u.name or "").strip().lower() == want), None)
