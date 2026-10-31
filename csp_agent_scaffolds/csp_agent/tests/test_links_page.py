"""Links page (app/link_health.py, GET /api/hub/links): clicks, the journey,
problems by side, who needs attention, one CSP's links and timeline, and an
RM seeing only their own CSPs."""
from datetime import datetime, timezone

from app import auth
from app.models import CSP, AgreementEvent, OutboundMessage, OutboundStatus
from tests.test_login import CSRF, PUBLIC, _login, world  # noqa: F401  (fixture)
from tests.test_portal_and_api import _csp, client  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}


def _stuck_csp(c, Session):
    """A CSP missing documents: sent a link, opened it twice, then a send
    failed on our side."""
    cid, code, token = _csp(Session)
    s = Session()
    s.get(CSP, cid).category = 3
    s.add(OutboundMessage(csp_id=cid, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876500000",
                          status=OutboundStatus.SENT, sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
                          payload_json={"link": f"https://x/upload?token={token}"}, recipient_role="CSP"))
    s.commit()
    s.close()
    for _ in range(2):
        assert c.get(f"/upload?token={token}").status_code == 200
    c.post("/upload", data={"action": "page_failure", "token": token, "kind": "server", "status": "502",
                            "sections": "agreement", "mb": "2.4"})
    return cid, code, token


def test_clicks_are_counted_in_one_record_per_hour(client):
    c, Session = client
    cid, _, _ = _stuck_csp(c, Session)
    s = Session()
    opens = s.query(AgreementEvent).filter(AgreementEvent.csp_id == cid, AgreementEvent.event_type == "LINK_OPENED").all()
    s.close()
    assert len(opens) == 1 and opens[0].payload["clicks"] == 2


def test_links_page_shows_the_journey_problems_and_who_is_stuck(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    cid, code, _ = _stuck_csp(c, Session)
    d = c.get("/api/hub/links?days=7", headers=HDR).json()
    steps = dict(d["journey"])
    assert steps["Link sent"] >= 1 and steps["Opened"] >= 1 and d["tiles"]["clicks"] >= 2
    assert d["by_side"]["OUR"] >= 1 and any("Server error" in k for k, _, _ in d["failed_kinds"])
    row = next(r for r in d["attention"] if r["csp"]["id"] == cid)
    assert row["side"] == "OUR" and "Server error" in row["what"]
    assert "dc_link" in d                                     # admins see the DC master link


def test_one_csps_links_and_timeline(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    cid, _, token = _stuck_csp(c, Session)
    d = c.get(f"/api/hub/links/csp/{cid}", headers=HDR).json()
    [link] = d["links"]
    assert link["ref"] == token[:6] and link["active"] and link["sent"] == 1 and link["clicks"] == 2
    assert link["failed"] == 1 and token not in str(d)       # never the whole link
    whats = [r["what"] for r in d["timeline"]]
    assert "Opened the link" in whats and any(w.startswith("Link sent by") for w in whats)
    assert d["last_problem"]["side"] == "OUR"


def test_an_rm_sees_only_their_own_csps_links(world):
    c, w = world
    _login(c, w["rm"])
    assert c.get(f"/api/hub/links/csp/{w['mine'][0]}", headers={**PUBLIC, **CSRF}).status_code == 200
    assert c.get(f"/api/hub/links/csp/{w['theirs'][0]}", headers={**PUBLIC, **CSRF}).status_code == 404
    assert "dc_link" not in c.get("/api/hub/links", headers={**PUBLIC, **CSRF}).json()
