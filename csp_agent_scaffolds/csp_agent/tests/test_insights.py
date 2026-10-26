"""Today page, CSP journey, Agent Coach, search, 'send reminder now' and the
dashboard action log."""
import uuid
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app import auth, insights
from app.comms import outbound
from app.main import app
from app.models import (AgreementEvent, CSP, CspQuestion, Document, DocumentStatus, InternalUser, ManualReviewQueue,
                        OutboundMessage, OutboundStatus, ReviewStatus, UserAction)
from tests.test_login import CSRF, PW, world  # noqa: F401  (fixture)
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}


def _csp(s, **kw):
    code = f"5T{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name=kw.pop("name", f"Today {code}"), current_code=code, lookup_code=code, phone="9876500011",
            whatsapp_number="9876500011", is_active_in_calling_sheet=True, category=kw.pop("category", 4), **kw)
    s.add(c)
    s.flush()
    return c


def test_today_lists_who_needs_a_person_and_why(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    refused = _csp(s, name="Refused Person")
    asking = _csp(s, name="Asking Person")
    ending = _csp(s, name="Ending Person", category=1)
    s.add(AgreementEvent(csp_id=refused.id, event_type="PORTAL_UPLOAD", source="CSP_UPLOAD_PORTAL", channel="WEB",
                         payload={"results": [{"section": "agreement", "ok": False, "reason": "expired",
                                               "kept_file": "x/y.pdf"}]}))
    s.add(CspQuestion(csp_id=asking.id, text="My link does not open", category="OTHER", status="OPEN",
                      created_at=datetime.now() - timedelta(days=3)))
    for t in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE"):
        s.add(Document(csp_id=ending.id, document_type=t, status=DocumentStatus.VALID, is_current=True,
                       readability="READABLE", issue_date=date.today() - timedelta(days=360),
                       expiry_date=date.today() + (timedelta(days=4) if t == "POLICE_VERIFICATION" else timedelta(days=500)),
                       sha256=uuid.uuid4().hex))
    s.commit()
    ids = {refused.id: "refused", asking.id: "question", ending.id: "expiring"}
    s.close()
    d = c.get("/api/hub/today", headers=HDR).json()
    found = {i["csp"]["id"]: i for i in d["attention"] if i["csp"] and i["csp"]["id"] in ids}
    assert {k: v["kind"] for k, v in found.items()} == ids
    assert any(a["do"] == "take" for a in found[refused.id]["actions"])
    assert "Police Verification" in found[ending.id]["why"] and found[ending.id]["actions"][0]["do"] == "remind"
    assert d["goal"]["total"] >= 3 and "tiles" in d
    assert d["attention"][0]["priority"] == 1                             # most urgent first


def test_journey_counts_what_csps_did_after_a_reminder(db_session):
    db = db_session
    a, b = _csp(db), _csp(db)
    sent = datetime.utcnow() - timedelta(days=2)
    for x in (a, b):
        db.add(OutboundMessage(csp_id=x.id, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876500011",
                               status=OutboundStatus.SENT, sent_at=sent, recipient_role="CSP", idempotency_key=uuid.uuid4().hex))
    db.add(AgreementEvent(csp_id=a.id, event_type="LINK_OPENED", channel="WEB", sent_at=datetime.now() - timedelta(days=1)))
    db.add(AgreementEvent(csp_id=a.id, event_type="PORTAL_UPLOAD", channel="WEB", sent_at=datetime.now() - timedelta(hours=20),
                          payload={"results": [{"section": "pvr", "ok": True}]}))
    db.flush()
    scope = CSP.id.in_([a.id, b.id])
    j = insights.journey(db, scope, 14)
    assert [s["n"] for s in j["steps"]][:4] == [2, 1, 1, 1]
    assert {x["label"]: x["n"] for x in j["reminders_before_upload"]}["1"] == 1


def test_coach_findings_and_ask(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    x = _csp(s)
    for _ in range(25):
        s.add(OutboundMessage(csp_id=x.id, template_name="ONBOARD_ALL", channel="WHATSAPP", destination="9876500011",
                              status=OutboundStatus.FAILED, recipient_role="CSP", idempotency_key=uuid.uuid4().hex,
                              error_log="Bulk Sender: There is already Hindi content for this template."))
    s.commit()
    s.close()
    d = c.get("/api/hub/coach", headers=HDR).json()
    f = next(f for f in d["findings"] if "onboard all" in f["title"])
    assert f["level"] == "critical" and "new template name" in f["detail"]
    levels = [f["level"] for f in d["findings"]]
    assert levels == sorted(levels, key=["critical", "warn", "info", "good"].index)      # most urgent first
    for q in d["questions"]:
        r = c.post("/api/hub/coach/ask", headers=HDR, json={"question": q}).json()
        assert r["answer"]
    assert "WhatsApp" in c.post("/api/hub/coach/ask", headers=HDR, json={"question": "are whatsapp messages failing"}).json()["answer"]


def test_search_and_send_reminder_now(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    x = _csp(s, name="Searchable Sunita")
    s.commit()
    code, cid = x.current_code, x.id
    s.close()
    assert c.get(f"/api/hub/search?q={code.lower()}", headers=HDR).json()["rows"][0]["id"] == cid
    assert any(r["id"] == cid for r in c.get("/api/hub/search?q=searchable", headers=HDR).json()["rows"])
    sent = []
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    monkeypatch.setattr(outbound, "WHATSAPP_DAILY_LIMIT", 10_000)
    from app.comms.whatsapp import SendOutcome
    monkeypatch.setattr(outbound, "send_wabs", lambda *a, **k: sent.append(a[2]) or SendOutcome("SENT", provider_message_id="1"))
    r = c.post(f"/api/hub/csp/{cid}/send-reminder", headers=HDR).json()
    assert r["status"] == "SENT" and r["template"] == "UPLOAD_MISSING" and sent == ["UPLOAD_MISSING"]


def test_dashboard_actions_are_recorded_with_who_and_which_csp(world):
    from tests.test_login import PUBLIC, _login
    c, w = world
    assert _login(c, w["admin"]).status_code == 200
    h = {**PUBLIC, **CSRF}
    c.get("/api/hub/today", headers=h)
    c.get(f"/api/hub/csp/{w['mine'][0]}", headers=h)
    c.get("/api/hub/summary", headers=h)                                 # the sidebar refresh: not recorded
    acts = c.get("/api/hub/activity?days=1", headers=h).json()
    mine = [r for r in acts["rows"] if r["role"] == "ADMIN"]
    whats = [r["what"] for r in mine]
    assert "Opened Today" in whats and "Opened a CSP's page" in whats
    assert any(r["csp"] and r["csp"]["code"] == w["mine"][1] for r in mine)
    assert not any("summary" in x.lower() for x in whats)
