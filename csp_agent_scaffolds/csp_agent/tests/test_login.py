"""Dashboard login (app/auth.py, app/api/login.py): admins see everything,
an RM only their own CSPs; requests from the internet always need a login."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import auth
from app.db import get_db
from app.main import app
from app.models import CSP, InternalUser

# Through Nginx (from the internet). The real public path prefix is checked
# in test_login_sets_a_safe_cookie...; elsewhere the cookie path is "/" so the
# test client (which calls /api/... directly) sends it back.
PUBLIC = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
PREFIX = {"X-Forwarded-Prefix": "/csp-agreement-agent/admin"}
CSRF = {"X-Requested-With": auth.CSRF_HEADER_VALUE}
PW = "Correct-Horse-42"


@pytest.fixture()
def world(test_engine, monkeypatch):
    Session = sessionmaker(bind=test_engine)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "server-only-key")
    s = Session()
    tag = uuid.uuid4().hex[:6]
    admin = InternalUser(name="Admin", email=f"admin{tag}@eko.co.in", role="ADMIN", login_enabled=True,
                         password_hash=auth.hash_password(PW))
    rm = InternalUser(name="RM One", email=f"rm{tag}@eko.co.in", role="RM", login_enabled=True,
                      password_hash=auth.hash_password(PW))
    other = InternalUser(name="RM Two", email=f"rm2{tag}@eko.co.in", role="RM")
    s.add_all([admin, rm, other])
    s.flush()
    mine = CSP(name="Mine", current_code=f"3M{int(tag, 16) % 1000000:06d}", rm_id=rm.id, category=4,
               is_active_in_calling_sheet=True)
    theirs = CSP(name="Theirs", current_code=f"3T{int(tag, 16) % 1000000:06d}", rm_id=other.id, category=4,
                 is_active_in_calling_sheet=True)
    s.add_all([mine, theirs])
    s.commit()
    w = {"admin": admin.email, "rm": rm.email, "mine": (mine.id, mine.current_code),
         "theirs": (theirs.id, theirs.current_code)}
    s.close()
    yield TestClient(app, base_url="https://testserver"), w
    app.dependency_overrides.clear()


def _login(c, email, pw=PW):
    return c.post("/auth/login", json={"email": email, "password": pw}, headers={**PUBLIC, **CSRF})


def test_internet_requests_need_a_login_and_the_server_key_does_not_work_there(world):
    c, w = world
    assert c.get("/api/hub/summary", headers=PUBLIC).status_code == 401
    assert c.get("/api/hub/summary", headers={**PUBLIC, "X-API-Key": "server-only-key"}).status_code == 401
    assert c.get("/api/hub/summary", headers={"X-API-Key": "server-only-key"}).status_code == 200   # on the server
    assert c.get("/docs", headers=PUBLIC).status_code == 404                                       # no API listing


def test_login_sets_a_safe_cookie_and_logout_ends_it(world):
    c, w = world
    r = c.post("/auth/login", json={"email": w["admin"], "password": PW}, headers={**PUBLIC, **PREFIX, **CSRF})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "secure" in cookie
    assert "path=/csp-agreement-agent/admin/" in cookie          # only this app on the shared domain
    r = _login(c, w["admin"])
    assert r.status_code == 200 and r.json()["role"] == "ADMIN"
    assert c.get("/api/hub/summary", headers=PUBLIC).json()["me"]["role"] == "ADMIN"
    c.post("/auth/logout", headers={**PUBLIC, **CSRF})
    assert c.get("/api/hub/summary", headers=PUBLIC).status_code == 401


def test_wrong_passwords_lock_the_account(world):
    c, w = world
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert _login(c, w["rm"], "wrong-Password-1").status_code == 401
    r = _login(c, w["rm"])                       # right password, but now locked
    assert r.status_code == 401 and "locked" in r.json()["detail"]
    assert _login(c, "nobody@eko.co.in").json()["detail"] == r.json()["detail"]   # same message: no hint


def test_an_rm_sees_only_their_own_csps(world):
    c, w = world
    _login(c, w["rm"])
    rows = c.get("/api/hub/csps?category=4&size=200", headers=PUBLIC).json()["rows"]
    codes = {r["code"] for r in rows}
    assert w["mine"][1] in codes and w["theirs"][1] not in codes
    assert c.get(f"/api/hub/csp/{w['mine'][0]}", headers=PUBLIC).status_code == 200
    assert c.get(f"/api/hub/csp/{w['theirs'][0]}", headers=PUBLIC).status_code == 404
    for admin_only in ("/api/hub/contacts", "/api/hub/inbound", "/api/hub/unmatched", "/api/hub/accuracy",
                       "/api/hub/reports/csp.xlsx"):
        assert c.get(admin_only, headers=PUBLIC).status_code == 403, admin_only


def test_changes_need_the_dashboard_header(world):
    c, w = world
    _login(c, w["admin"])
    assert c.post(f"/api/hub/csp/{w['mine'][0]}/upload-link", headers=PUBLIC).status_code == 403
    assert c.post(f"/api/hub/csp/{w['mine'][0]}/upload-link", headers={**PUBLIC, **CSRF}).status_code == 200


def test_weak_passwords_are_refused():
    assert auth.password_problem("short1A")
    assert auth.password_problem("alllowercase123")
    assert auth.password_problem(PW) is None
    assert auth.verify_password(PW, auth.hash_password(PW)) and not auth.verify_password("x", auth.hash_password(PW))
