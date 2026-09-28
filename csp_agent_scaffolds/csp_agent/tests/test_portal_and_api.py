"""Portal uploads and API access, end to end through FastAPI, on the test DB.
Documents are generated on the fly; no real CSP files are used."""
import io
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import vault
from app import auth
from app.db import get_db
from app.main import app
from app.models import CSP, Document, InternalUser, PortalToken
from app.portal_tokens import issue_upload_link, resolve_token


@pytest.fixture()
def client(test_engine, tmp_path, monkeypatch):
    TestSession = sessionmaker(bind=test_engine)

    def _db():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _db
    yield TestClient(app), TestSession
    app.dependency_overrides.clear()


def _csp(Session):
    s = Session()
    tag = uuid.uuid4().hex[:6]
    code = f"8Y{int(tag, 16) % 1000000:06d}"
    rm = InternalUser(name=f"RM {tag}", role="RM", phone="9000000011")
    s.add(rm)
    s.flush()
    c = CSP(name=f"Portal CSP {tag}", current_code=code, lookup_code=code, phone="9876500000",
            whatsapp_number="9876500000", rm_id=rm.id, is_active_in_calling_sheet=True)
    s.add(c)
    s.flush()
    link = issue_upload_link(s, c, ["POLICE_VERIFICATION", "IIBF_CERTIFICATE", "AGREEMENT"])
    s.commit()
    token = link.split("token=")[1]
    cid = c.id
    s.close()
    return cid, code, token


def _pvr_pdf(issue: date) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    text = ("Government of Uttar Pradesh  CHARACTER CERTIFICATE\n"
            f"Application No. - 202503277853 Date - {issue.strftime('%d-%m-%Y')}\n"
            "This is to certify that Mr. TEST PERSON ... no adverse entry was found against the said\n"
            "candidate in the police records. This certificate is valid only for one year.\n"
            "This is a computer generated document so no signature is required.\n"
            f"Digitally signed by TEST OFFICER Date: {issue.strftime('%Y.%m.%d')} 18:29:17 +05'30'\n")
    page.insert_text((50, 80), text, fontsize=10)
    return doc.tobytes()


def _blurry_png() -> bytes:
    import numpy as np
    import cv2
    img = np.full((1400, 1000), 255, np.uint8)
    cv2.putText(img, "POLICE VERIFICATION 12/05/2025", (40, 400), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 0, 3)
    img = cv2.GaussianBlur(img, (61, 61), 30)
    return cv2.imencode(".png", img)[1].tobytes()


def _form(token, **kw):
    base = {"token": token, "name": "Portal CSP", "mobile": "9876500000"}
    base.update(kw)
    return base


def test_bad_token_page_is_403(client):
    c, _ = client
    assert c.get("/upload?token=nope").status_code == 403


def test_page_renders_with_escaped_context(client):
    c, Session = client
    _, code, token = _csp(Session)
    r = c.get(f"/upload?token={token}")
    assert r.status_code == 200 and code in r.text and "__CONTEXT_JSON__" not in r.text


def test_readable_pvr_is_accepted_and_filed_in_csp_folder(client, tmp_path):
    c, Session = client
    cid, code, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["issue_date"] == issue.isoformat()
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.readability == "READABLE" and d.is_current and d.date_source == "PVR_DIGITAL_SIGNATURE_DATE"
    path = vault.abs_path(d.storage_path)
    assert path.parent.parent == tmp_path / "documents" and path.parent.name.startswith(code + "_")
    assert path.exists() and path.name.startswith(f"{code}_PVR_ACTIVE_{issue.isoformat()}_to_")
    assert not d.storage_path.startswith("/")   # stored relative to the vault
    s.close()


def test_expired_typed_date_is_rejected_before_reading(client):
    c, Session = client
    _, _, token = _csp(Session)
    old = date.today() - timedelta(days=500)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=old.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(old), "application/pdf")})
    res = r.json()["results"][0]
    assert not res["ok"] and "expired" in res["message"]["en"].lower()


def test_future_date_is_rejected(client):
    c, Session = client
    _, _, token = _csp(Session)
    future = date.today() + timedelta(days=3)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=future.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(date.today()), "application/pdf")})
    assert not r.json()["results"][0]["ok"]


def test_blurry_photo_is_rejected_and_counts_as_missing(client, monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: None)   # no vision model in tests
    c, Session = client
    cid, _, token = _csp(Session)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=date.today().isoformat()),
               files={"pvr_file": ("pvr.png", _blurry_png(), "image/png")})
    res = r.json()["results"][0]
    assert not res["ok"] and "scanned pdf" in res["message"]["en"].lower()
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.readability == "UNREADABLE" and not d.is_current and "/unreadable/" in d.storage_path
    assert "_PVR_UNREADABLE_received_" in d.storage_path or "_UNKNOWN_UNREADABLE_received_" in d.storage_path
    s.close()


def test_fake_file_type_rejected(client):
    c, Session = client
    _, _, token = _csp(Session)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=date.today().isoformat()),
               files={"pvr_file": ("pvr.pdf", b"<html><script>alert(1)</script></html>", "application/pdf")})
    assert not r.json()["results"][0]["ok"]


def test_new_message_reuses_the_live_link(client):
    """A reminder drafted later must not kill the link the CSP already has."""
    _, Session = client
    cid, _, token = _csp(Session)
    s = Session()
    csp = s.get(CSP, cid)
    row = resolve_token(s, token)
    row.requested_types = ["POLICE_VERIFICATION"]
    s.commit()
    again = issue_upload_link(s, csp, ["AGREEMENT"])
    s.commit()
    assert again.endswith(token) and resolve_token(s, token) is not None
    assert set(resolve_token(s, token).requested_types) == {"POLICE_VERIFICATION", "AGREEMENT"}
    s.close()


def _approved_whatsapp(Session, cid, link="https://x/upload?token=abc"):
    from app.models import OutboundMessage, OutboundStatus
    s = Session()
    m = OutboundMessage(csp_id=cid, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876500000",
                        payload_json={"subject": "", "body": "hello", "link": link}, status=OutboundStatus.APPROVED,
                        idempotency_key=uuid.uuid4().hex, recipient_role="CSP", attempts=0)
    s.add(m)
    s.commit()
    mid = m.id
    s.close()
    return mid


def test_agent_pull_includes_link_and_is_not_listed_twice(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    cid, _, _ = _csp(Session)
    mid = _approved_whatsapp(Session, cid)
    h = {"X-Agent-Key": "agent-key"}
    first = [m for m in c.get("/api/agent/outbox", headers=h).json()["messages"] if m["id"] == mid]
    assert first and first[0]["link"] == "https://x/upload?token=abc"
    from app.comms.outbound import process_outbox
    s = Session()
    process_outbox(s)          # the 2-minute worker job must leave a pulled message alone
    s.close()
    again = [m for m in c.get("/api/agent/outbox", headers=h).json()["messages"] if m["id"] == mid]
    assert again == []
    r = c.post(f"/api/agent/outbox/{mid}/ack", headers={"Authorization": "Bearer agent-key"}, json={"status": "SENT"})
    assert r.status_code == 200 and r.json()["status"] == "SENT"


def test_agent_can_fetch_a_csps_link(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    cid, code, token = _csp(Session)
    r = c.get(f"/api/agent/csp/{code}/link", headers={"X-Agent-Key": "agent-key"})
    assert r.status_code == 200 and r.json()["link"].endswith(token)


def test_contact_edit_is_queued_not_applied(client):
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=5)
    c.post("/api/portal/upload", data=_form(token, mobile="9123456789", pvr_issue_date=issue.isoformat()),
           files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    s = Session()
    assert s.get(CSP, cid).phone == "9876500000"
    from app.models import ContactChangeRequest
    assert s.query(ContactChangeRequest).filter_by(csp_id=cid, field="mobile").one().new_value == "9123456789"
    s.close()


def test_admin_api_needs_key_when_set(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "secret-test-key")
    assert c.get("/api/hub/summary").status_code == 401
    assert c.get("/api/hub/summary", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/hub/summary", headers={"X-API-Key": "secret-test-key"}).status_code == 200


def test_agent_api_needs_its_own_key(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(auth, "AGENT_API_KEY", "agent-key")
    assert c.get("/api/agent/outbox").status_code == 401
    assert c.get("/api/agent/outbox", headers={"X-Agent-Key": "agent-key"}).status_code == 200
