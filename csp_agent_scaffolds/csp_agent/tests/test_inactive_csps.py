"""CSPs whose terminal is Inactive on the calling sheet: their own label,
never in Labels 1-4, never messaged."""
import uuid
from datetime import date

from app import auth
from app.compliance import INACTIVE_LABEL, is_inactive
from app.comms import outbound
from app.models import CSP, OutboundMessage, OutboundStatus
from tests.test_portal_and_api import client  # noqa: F401


def _csp(db, status="Inactive"):
    code = f"9I{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Inactive CSP", current_code=code, lookup_code=code, phone="9876543210",
            whatsapp_number="9876543210", is_active_in_calling_sheet=True, terminal_status=status)
    db.add(c)
    db.flush()
    return c


def test_terminal_status_words():
    assert is_inactive(CSP(terminal_status=" Inactive ")) and is_inactive(CSP(terminal_status="CLOSED"))
    assert not is_inactive(CSP(terminal_status="Active")) and not is_inactive(CSP(terminal_status=None))


def test_no_message_is_queued_or_sent(db_session):
    db = db_session
    c = _csp(db)
    [m] = outbound.draft(db, csp=c, role="CSP", template_key="ONBOARD_ALL", ctx={"csp_name": c.name, "csp_code": c.current_code, "docs": [], "upload_link": "x"},
                         key_base=f"t{c.id}", channels=("WHATSAPP",))
    assert m.status == OutboundStatus.BLOCKED and "Inactive" in m.error_log
    queued = OutboundMessage(csp_id=c.id, template_name="ONBOARD_ALL", channel="WHATSAPP", destination="9876543210",
                             status=OutboundStatus.QUEUED_FOR_REVIEW, recipient_role="CSP", payload_json={})
    approved = OutboundMessage(csp_id=c.id, template_name="ONBOARD_ALL", channel="WHATSAPP", destination="9876543210",
                               status=OutboundStatus.APPROVED, recipient_role="CSP", payload_json={})
    db.add_all([queued, approved])
    db.commit()
    assert outbound.block_inactive(db) >= 2
    db.refresh(queued)
    db.refresh(approved)
    assert queued.status == approved.status == OutboundStatus.BLOCKED


def test_daily_run_drafts_nothing_for_an_inactive_csp(db_session):
    from app.renewal_engine import run_for_csp
    db = db_session
    c = _csp(db)
    db.commit()
    run_for_csp(db, c, date(2026, 10, 19))
    db.commit()
    sendable = db.query(OutboundMessage).filter(OutboundMessage.csp_id == c.id,
                                                OutboundMessage.status != OutboundStatus.BLOCKED).count()
    assert sendable == 0


def test_inactive_label_on_the_dashboard(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    gone, works = _csp(s, "Inactive"), _csp(s, "Active")
    gone.category = works.category = 4
    s.commit()
    gone_id, works_id = gone.id, works.id
    s.close()
    key = {"X-API-Key": "k"}
    in5 = {r["id"] for r in c.get(f"/api/hub/csps?category={INACTIVE_LABEL}&size=200", headers=key).json()["rows"]}
    in4 = {r["id"] for r in c.get("/api/hub/csps?category=4&size=200", headers=key).json()["rows"]}
    assert gone_id in in5 and gone_id not in in4 and works_id in in4 and works_id not in in5
    assert c.get("/api/hub/summary", headers=key).json()["categories"][str(INACTIVE_LABEL)] >= 1


def test_whatsapp_number_set_on_the_dashboard_wins_over_the_sheet(client, monkeypatch):
    """Nusrat's case: the sheet's main number is not in use; the CSP asked for her other number."""
    from app.comms.sheets_sync import clean_phones  # noqa: F401  (the sheet keeps its own numbers)
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    hdr = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
    c, Session = client
    s = Session()
    csp = _csp(s, "Active")
    csp.phone, csp.whatsapp_number, csp.alt_phone = "8421293070", "8421293070", "9975051660"
    old = OutboundMessage(csp_id=csp.id, template_name="ONBOARD_ALL", channel="WHATSAPP", destination="8421293070",
                          status=OutboundStatus.QUEUED_FOR_REVIEW, recipient_role="CSP", payload_json={})
    s.add(old)
    s.commit()
    cid, old_id = csp.id, old.id
    s.close()
    assert c.post(f"/api/hub/csp/{cid}/whatsapp-number", json={"phone": "12345"}, headers=hdr).status_code == 422
    r = c.post(f"/api/hub/csp/{cid}/whatsapp-number", json={"phone": "9975051660"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["preferred_phone"] == "9975051660" and r.json()["retired"] == 1
    assert all(w == "9975051660" for w in r.json()["waiting"])
    s = Session()
    assert s.get(OutboundMessage, old_id).status == OutboundStatus.REJECTED
    assert outbound.allowed_destinations(s, s.get(CSP, cid), "CSP", "WHATSAPP") == ["9975051660"]
    s.close()
