"""
Who may use the dashboard and the admin API.

  Logged-in users (the dashboard, also on the public address)
      ADMIN  everything
      RM     only their own CSPs (CSP.rm_id), and no system-wide pages
      Accounts are internal_users rows with login_enabled and a password
      (scripts/manage_users.py). Login = email + password.
  X-API-Key: ADMIN_API_KEY
      scripts and tools ON THE SERVER only: refused for any request that came
      through Nginx (it carries X-Forwarded-For), so a leaked key is useless
      from the internet.
  X-Agent-Key: AGENT_API_KEY   /api/agent/* (the WhatsApp agent)
  public                        /upload, /api/portal/*, /health, the login page

Safety:
  - passwords: scrypt with a per-user salt, never stored or logged in clear;
  - 5 wrong passwords lock the account for 15 minutes; one generic error
    message, so it doesn't reveal which emails exist;
  - session cookie: random 32 bytes, HttpOnly, SameSite=Strict, Secure over
    HTTPS, scoped to this app's path; only its SHA-256 is stored; expires
    after SESSION_IDLE_MINUTES idle or SESSION_MAX_HOURS in total;
  - every state-changing request with a cookie must carry the
    X-Requested-With header the dashboard sends (a cross-site form can't).
"""
import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import ADMIN_API_KEY, AGENT_API_KEY
from .db import get_db

logger = logging.getLogger(__name__)

COOKIE = "csp_agent_session"
SESSION_IDLE_MINUTES = 60
SESSION_MAX_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
LOGIN_ROLES = ("ADMIN", "RM")
CSRF_HEADER_VALUE = "csp-dashboard"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    try:
        algo, n, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> Optional[str]:
    if len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Use upper- and lower-case letters and at least one digit."
    return None


# -------------------------------------------------------------- identity
@dataclass
class Principal:
    name: str                       # shown in audit trails ("approved by")
    role: str                       # ADMIN or RM
    user_id: Optional[int] = None   # internal_users.id (the RM's id for scoping)

    @property
    def is_admin(self) -> bool:
        return self.role == "ADMIN"


def _forwarded(request: Request) -> bool:
    """True when the request came through Nginx, i.e. from outside the server."""
    return bool(request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"))


def cookie_path(request: Request) -> str:
    return request.headers.get("x-forwarded-prefix", "").rstrip("/") + "/" or "/"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user, request: Request) -> str:
    from .models import AdminSession
    token = secrets.token_urlsafe(32)
    now = _now()
    db.add(AdminSession(token_hash=_hash_token(token), user_id=user.id, created_at=now, last_seen_at=now,
                        expires_at=now + timedelta(hours=SESSION_MAX_HOURS),
                        ip=(request.headers.get("x-real-ip") or (request.client.host if request.client else ""))[:64]))
    return token


def end_session(db: Session, token: str) -> None:
    from .models import AdminSession
    db.query(AdminSession).filter(AdminSession.token_hash == _hash_token(token)).delete()


def _session_user(db: Session, token: str):
    from .models import AdminSession, InternalUser
    row = db.get(AdminSession, _hash_token(token))
    now = _now()
    if row is None:
        return None
    if row.expires_at < now or row.last_seen_at < now - timedelta(minutes=SESSION_IDLE_MINUTES):
        db.delete(row)
        db.commit()
        return None
    user = db.get(InternalUser, row.user_id)
    if user is None or not user.login_enabled or (user.role or "").upper() not in LOGIN_ROLES:
        return None
    if row.last_seen_at < now - timedelta(minutes=1):
        row.last_seen_at = now
        db.commit()
    return user


def current_principal(request: Request, db: Session = Depends(get_db),
                      x_api_key: str = Header(default=""),
                      x_requested_with: str = Header(default="")) -> Principal:
    token = request.cookies.get(COOKIE, "")
    if token:
        user = _session_user(db, token)
        if user is not None:
            if request.method not in ("GET", "HEAD", "OPTIONS") and x_requested_with != CSRF_HEADER_VALUE:
                raise HTTPException(status_code=403, detail="Missing X-Requested-With header.")
            return Principal(user.email or user.name, (user.role or "").upper(), user.id)
    if x_api_key and ADMIN_API_KEY and _eq(x_api_key, ADMIN_API_KEY):
        if _forwarded(request):
            raise HTTPException(status_code=401, detail="Please log in.")
        return Principal("admin-key", "ADMIN")
    if not ADMIN_API_KEY and not _forwarded(request):
        return Principal("local-dev", "ADMIN")       # a laptop before .env is set up
    raise HTTPException(status_code=401, detail="Please log in.")


def require_admin(principal: Principal = Depends(current_principal)) -> str:
    """Admins only. Returns the name for audit trails."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Admins only.")
    return principal.name


def require_user(principal: Principal = Depends(current_principal)) -> Principal:
    """Any logged-in user (ADMIN or RM)."""
    return principal


def require_agent(x_agent_key: str = Header(default=""), authorization: str = Header(default="")) -> str:
    """The WhatsApp agent sends AGENT_API_KEY as X-Agent-Key, or as
    "Authorization: Bearer <key>" (easier for delivery callbacks)."""
    if not AGENT_API_KEY:
        raise HTTPException(status_code=503, detail="AGENT_API_KEY is not configured.")
    key = x_agent_key or (authorization[7:].strip() if authorization[:7].lower() == "bearer " else "")
    if not key or not _eq(key, AGENT_API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-Agent-Key.")
    return "whatsapp-agent"


def warn_if_open() -> None:
    if not ADMIN_API_KEY:
        logger.warning("ADMIN_API_KEY is not set: on this machine the admin API is open without a login. "
                       "Requests through Nginx always need a login.")
