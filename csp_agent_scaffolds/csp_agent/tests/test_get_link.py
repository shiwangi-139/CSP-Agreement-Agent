"""The public "get your upload link" page (app/api/portal.py): typing the bare
address no longer dead-ends, and a fresh link goes only to a number on the
calling sheet, never onto the screen."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.api import portal
from app.comms import outbound, wabs
from app.db import get_db
from app.main import app
from app.models import CSP, OutboundMessage, OutboundStatus

PUBLIC = {"X-Forwarded-For": "203.0.113.9", "X-Real-IP": "203.0.113.9"}


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
    monkeypatch.setattr(portal, "SessionLocal", Session)
    portal._ip_hits.clear()
    s = Session()
    code = f"9L{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Link Tester", current_code=code, lookup_code=code, phone="9876501234",
              whatsapp_number="9876501234", is_active_in_calling_sheet=True, category=4)
    s.add(csp)
    s.commit()
    w = {"id": csp.id, "code": code, "Session": Session}
    s.close()
    yield TestClient(app), w
    app.dependency_overrides.clear()


def test_bare_address_shows_the_get_link_form(world):
    c, _ = world
    r = c.get("/upload", headers=PUBLIC)
    assert r.status_code == 200 and "Get your upload link" in r.text and "अब नहीं चल रहा" not in r.text
    r = c.get("/upload?token=not-a-real-token", headers=PUBLIC)
    assert r.status_code == 200 and "अब नहीं चल रहा" in r.text and 'name="mobile"' in r.text


def test_link_is_sent_only_when_code_and_number_match(world, monkeypatch):
    c, w = world
    calls = []
    monkeypatch.setattr(portal, "_send_link", lambda csp_id, phone: calls.append((csp_id, phone)))
    good = c.post("/upload", data={"code": w["code"].lower(), "mobile": "98765 01234"}, headers=PUBLIC)
    wrong_number = c.post("/upload", data={"code": w["code"], "mobile": "9111111111"}, headers=PUBLIC)
    unknown = c.post("/upload", data={"code": "1Z999999", "mobile": "9876501234"}, headers=PUBLIC)
    assert calls == [(w["id"], "9876501234")]
    # the same answer every time: the page never reveals who is on record
    texts = {r.text.split("<main>")[1] for r in (good, wrong_number, unknown)}
    assert len(texts) == 1 and "upload?token=" not in good.text


def test_requests_are_limited(world, monkeypatch):
    c, w = world
    monkeypatch.setattr(portal, "_send_link", lambda *a: None)
    for _ in range(portal.LINK_REQUESTS_PER_IP_HOUR):
        c.post("/upload", data={"code": "1Z999999", "mobile": "9876501234"}, headers=PUBLIC)
    r = c.post("/upload", data={"code": w["code"], "mobile": "9876501234"}, headers=PUBLIC)
    assert "Too many attempts" in r.text


def test_the_link_message_goes_to_the_typed_number_without_review(world, monkeypatch):
    _, w = world
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    sent = []
    monkeypatch.setattr(wabs, "send_clean", lambda kind, contacts, src, force=False:
                        sent.append((kind, contacts)) or {"action": "sent", "job_id": "j1"})
    portal._send_link(w["id"], "9876501234")
    s = w["Session"]()
    [m] = s.query(OutboundMessage).filter_by(csp_id=w["id"], template_name="LINK_REQUEST").all()
    assert m.status == OutboundStatus.SENT and m.destination == "9876501234"
    assert sent[0][0] == "link" and sent[0][1][0]["phone"] == "919876501234"
    assert "/upload?token=" in sent[0][1][0]["link"]
    s.close()
