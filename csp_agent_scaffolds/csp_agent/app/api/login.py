"""
Dashboard login (app/auth.py has the rules).

POST /auth/login    {email, password} -> session cookie
POST /auth/logout   ends the session
GET  /auth/me       who is logged in (the dashboard asks on load)
"""
import logging
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import auth
from ..db import get_db
from ..models import InternalUser

logger = logging.getLogger(__name__)
router = APIRouter()
GENERIC = "Wrong email or password, or the account is locked. Try again in a few minutes."


def _set_cookie(response: Response, request: Request, value: str, max_age: int) -> None:
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, secure=https, samesite="strict",
                        path=auth.cookie_path(request))


@router.post("/auth/login")
def login(request: Request, response: Response, email: str = Body(...), password: str = Body(...),
          db: Session = Depends(get_db)):
    now = auth._now()
    user = db.query(InternalUser).filter(func.lower(InternalUser.email) == email.strip().lower()).first()
    usable = (user is not None and user.login_enabled and (user.role or "").upper() in auth.LOGIN_ROLES
              and user.password_hash)
    if not usable:
        auth.verify_password(password, auth.hash_password("x"))   # same work either way: no timing hint
        logger.warning("login_failed unknown_or_disabled email=%s", email[:80])
        raise HTTPException(status_code=401, detail=GENERIC)
    if user.locked_until and user.locked_until > now:
        logger.warning("login_locked user=%s", user.id)
        raise HTTPException(status_code=401, detail=GENERIC)
    if not auth.verify_password(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= auth.MAX_FAILED_LOGINS:
            user.locked_until, user.failed_logins = now + timedelta(minutes=auth.LOCK_MINUTES), 0
            logger.warning("login_locked_after_failures user=%s", user.id)
        db.commit()
        raise HTTPException(status_code=401, detail=GENERIC)
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    token = auth.create_session(db, user, request)
    db.commit()
    _set_cookie(response, request, token, auth.SESSION_MAX_HOURS * 3600)
    logger.info("login_ok user=%s role=%s", user.id, user.role)
    return {"name": user.name, "email": user.email, "role": (user.role or "").upper()}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(auth.COOKIE, "")
    if token:
        auth.end_session(db, token)
        db.commit()
    _set_cookie(response, request, "", 0)
    return {"ok": True}


@router.get("/auth/me")
def me(principal: auth.Principal = Depends(auth.current_principal), db: Session = Depends(get_db)):
    user = db.get(InternalUser, principal.user_id) if principal.user_id else None
    return {"name": user.name if user else principal.name, "email": user.email if user else None,
            "role": principal.role}
