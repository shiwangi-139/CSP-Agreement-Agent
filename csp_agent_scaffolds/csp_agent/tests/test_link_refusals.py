"""Refused links are recorded with their reason (LINK_REFUSED), a link closed
after acceptance says so, and both upload pages name the cause of a failed
send (internet, file too big, our side) and check the size before sending."""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.api.portal import MSG
from app.config import MAX_UPLOAD_SIZE_BYTES
from app.models import AgreementEvent, PortalToken
from tests.test_master_link import _link
from tests.test_portal_and_api import _csp, _form, client  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _refusals(Session, link_ref):
    s = Session()
    rows = [(e.csp_id, e.source, dict(e.payload)) for e in s.query(AgreementEvent).filter(
        AgreementEvent.event_type == "LINK_REFUSED", AgreementEvent.payload["link"].astext == link_ref)]
    s.close()
    return rows


def _set_token(Session, token, **cols):
    s = Session()
    row = s.get(PortalToken, token)
    for k, v in cols.items():
        setattr(row, k, v)
    s.commit()
    s.close()


def test_unknown_link_is_recorded_once_an_hour_without_the_token(client):
    c, Session = client
    token = "nope" + uuid.uuid4().hex
    for _ in range(3):                                        # reloads
        assert "Get your upload link" in c.get(f"/upload?token={token}").text
    [(csp_id, source, p)] = _refusals(Session, hashlib.sha256(token.encode()).hexdigest()[:6])
    assert csp_id is None and source == "CSP_UPLOAD_PORTAL"
    assert p["reason"] == "UNKNOWN" and p["where"] == "page" and token not in str(p)


def test_link_closed_after_acceptance_says_so_and_is_kept_against_the_csp(client):
    c, Session = client
    cid, _, token = _csp(Session)
    _set_token(Session, token, revoked_at=_utcnow(), revoke_reason="ALL_REQUESTED_DOCUMENTS_ACCEPTED")
    assert "We already have every document this link asked for" in c.get(f"/upload?token={token}").text
    [(csp_id, _, p)] = _refusals(Session, token[:6])
    assert csp_id == cid and p["reason"] == "CLOSED" and p["closed_because"] == "ALL_REQUESTED_DOCUMENTS_ACCEPTED"
    assert token not in str(p)


def test_expired_link_is_refused_on_upload_with_its_reason(client):
    c, Session = client
    cid, _, token = _csp(Session)
    _set_token(Session, token, expires_at=_utcnow() - timedelta(days=1))
    r = c.post("/api/portal/upload", data=_form(token))
    assert r.status_code == 403 and "invalid or has expired" in r.json()["detail"]["en"]
    [(csp_id, _, p)] = _refusals(Session, token[:6])
    assert csp_id == cid and p["reason"] == "EXPIRED" and p["where"] == "upload" and p["expires_at"]


def test_replaced_dc_master_link_is_recorded(client):
    c, Session = client
    old = _link(Session)
    _link(Session)                                            # a new link switches the old one off
    assert "Get your upload link" in c.get(f"/upload?dc={old}").text
    [(csp_id, source, p)] = _refusals(Session, hashlib.sha256(old.encode()).hexdigest()[:6])
    assert csp_id is None and source == "DC_MASTER_LINK" and p["reason"] == "CLOSED" and p["closed_at"]


def test_too_big_message_names_the_limit():
    mb = round(MAX_UPLOAD_SIZE_BYTES / 1024 / 1024)
    assert f"{mb} MB" in MSG["too_big"][0] and f"{mb} MB" in MSG["too_big"][1]


def test_both_pages_check_size_first_and_report_failed_sends():
    for name in ("portal.html", "dc_master.html"):
        page = (WEB / name).read_text(encoding="utf-8")
        assert "f.size <=" in page                            # checked before sending
        assert "status === 413" in page and "status === 504" in page and "navigator.onLine" in page
        assert '"page_failure"' in page and "localStorage" in page
        assert "Network problem, please try again" not in page
        assert "server" not in page.split("const FAIL = {", 1)[1].split("};", 1)[0].lower()   # no technical cause


def _report(c, **kw):
    return c.post("/upload", data={"action": "page_failure", **kw})


def test_failed_send_reaches_the_dashboard_with_its_side(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    hdr = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
    c, Session = client
    cid, code, token = _csp(Session)
    r = _report(c, token=token, kind="slow", status="504", ms="61000", mb="6.2", sections="agreement,bogus",
                where="upload", late_s="0")
    assert r.status_code == 200 and r.json()["ok"]
    assert _report(c, token=token, kind="offline", ms="900", mb="1.0", sections="pvr", late_s="720").json()["ok"]
    s = Session()
    got = {e.payload["kind"]: (e.csp_id, e.payload) for e in s.query(AgreementEvent).filter(
        AgreementEvent.event_type == "UPLOAD_FAILED", AgreementEvent.csp_id == cid)}
    s.close()
    assert got["slow"][1]["side"] == "OUR" and got["slow"][1]["sections"] == ["agreement"]
    assert got["slow"][1]["seconds"] == 61 and got["slow"][1]["mb"] == 6.2 and got["slow"][1]["status"] == 504
    assert got["offline"][1]["side"] == "CSP" and got["offline"][1]["late_min"] == 12
    d = c.get("/api/hub/activity?days=1", headers=hdr).json()
    mine = [p for p in d["upload_problems"] if p["csp"] and p["csp"]["code"] == code]
    assert {p["side"] for p in mine} == {"OUR", "CSP"} and d["upload_problems_by_side"]["OUR"] >= 1
    assert any("too slow" in p["what"] and "6.2 MB" in p["details"] for p in mine)


def test_failure_report_needs_a_live_link_and_a_known_kind(client):
    c, Session = client
    cid, _, token = _csp(Session)
    assert not _report(c, token=token, kind="anything").json()["ok"]
    assert not _report(c, token="nope" + uuid.uuid4().hex, kind="slow").json()["ok"]
    s = Session()
    assert s.query(AgreementEvent).filter(AgreementEvent.event_type == "UPLOAD_FAILED",
                                          AgreementEvent.csp_id == cid).count() == 0
    s.close()
