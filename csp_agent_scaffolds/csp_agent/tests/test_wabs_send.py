"""WHATSAPP_MODE=wabs: approving a draft on the dashboard sends it through
the WhatsApp Bulk Sender, only for types with a Meta-approved layout and
never more than WHATSAPP_DAILY_LIMIT a day."""
import uuid

import pytest

from app.comms import outbound, wabs
from app.models import CSP, OutboundMessage, OutboundStatus


@pytest.fixture()
def wabs_mode(monkeypatch):
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    sent = []

    def fake_send(kind, contacts, source_name, force=False):
        sent.append((kind, contacts, source_name))
        return {"action": "sent", "job_id": "job1"}
    monkeypatch.setattr(wabs, "send_clean", fake_send)
    return sent


def _draft(db, template="ONBOARD_ALL", edited=False):
    code = f"9W{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Ram Kumar", current_code=code, phone="9876543210", is_active_in_calling_sheet=True, category=4)
    db.add(csp)
    db.flush()
    m = OutboundMessage(csp_id=csp.id, template_name=template, channel="WHATSAPP", destination="9876543210",
                        recipient_role="CSP", status=OutboundStatus.QUEUED_FOR_REVIEW, attempts=0,
                        idempotency_key=uuid.uuid4().hex,
                        payload_json={"body": "x", "link": "https://x/upload?token=t", **({"edited": True} if edited else {})})
    db.add(m)
    db.flush()
    return csp, m


def _allow_more(db, monkeypatch, n=1):
    start = outbound._now().replace(hour=0, minute=0, second=0, microsecond=0)
    used = db.query(OutboundMessage).filter(OutboundMessage.channel == "WHATSAPP", OutboundMessage.reviewed_at >= start,
                                            OutboundMessage.status.in_([OutboundStatus.APPROVED, OutboundStatus.SENT,
                                                                        OutboundStatus.FAILED])).count()
    monkeypatch.setattr(outbound, "WHATSAPP_DAILY_LIMIT", used + n)


def test_approve_sends_the_approved_layout_to_the_csp(db_session, wabs_mode, monkeypatch):
    csp, m = _draft(db_session)
    _allow_more(db_session, monkeypatch)
    outbound.approve(db_session, m, "tester")
    outbound.send(db_session, m)
    assert m.status == OutboundStatus.SENT and m.provider_message_id == "job1"
    kind, contacts, _ = wabs_mode[0]
    assert kind == "onboard" and contacts[0]["phone"] == "919876543210"
    assert contacts[0]["code"] == csp.current_code
    assert "/upload?token=" in contacts[0]["link"] and contacts[0]["link"] != "https://x/upload?token=t"  # dead link replaced


def test_daily_limit_stops_a_mass_send(db_session, wabs_mode, monkeypatch):
    _, a = _draft(db_session)
    _, b = _draft(db_session)
    _allow_more(db_session, monkeypatch, 1)
    outbound.approve(db_session, a, "tester")
    with pytest.raises(ValueError, match="Daily WhatsApp limit"):
        outbound.approve(db_session, b, "tester")
    assert b.status == OutboundStatus.QUEUED_FOR_REVIEW


def test_types_without_a_layout_or_edited_text_are_refused(db_session, wabs_mode, monkeypatch):
    _allow_more(db_session, monkeypatch, 5)
    _, m = _draft(db_session, template="UPLOAD_EXPIRED")
    with pytest.raises(ValueError, match="no WhatsApp layout"):
        outbound.approve(db_session, m, "tester")
    _, e = _draft(db_session, edited=True)
    with pytest.raises(ValueError, match="Edited text"):
        outbound.approve(db_session, e, "tester")


def test_waiting_for_meta_is_retried_not_lost(db_session, monkeypatch):
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    monkeypatch.setattr(wabs, "send_clean", lambda *a, **k: {"action": "pending"})
    _allow_more(db_session, monkeypatch)
    _, m = _draft(db_session)
    outbound.approve(db_session, m, "tester")
    outbound.send(db_session, m)
    assert m.status == OutboundStatus.APPROVED and m.next_retry_at is not None and "Meta" in m.error_log


def test_messages_approved_in_stub_mode_can_be_sent_once_connected(db_session, wabs_mode, monkeypatch):
    _, m = _draft(db_session)
    m.status, m.error_log = OutboundStatus.READY_NOT_SENT, "WhatsApp agent not connected yet (stub mode)."
    _allow_more(db_session, monkeypatch)
    outbound.send_parked(db_session, m, "tester")
    assert m.status == OutboundStatus.SENT and m.error_log is None and len(wabs_mode) == 1
    with pytest.raises(ValueError):                     # only once
        outbound.send_parked(db_session, m, "tester")


def test_parked_messages_still_obey_the_rules(db_session, wabs_mode, monkeypatch):
    _allow_more(db_session, monkeypatch, 5)
    _, m = _draft(db_session, template="UPLOAD_EXPIRED")
    m.status = OutboundStatus.READY_NOT_SENT
    with pytest.raises(ValueError, match="no WhatsApp layout"):
        outbound.send_parked(db_session, m, "tester")
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "stub")
    _, s = _draft(db_session)
    s.status = OutboundStatus.READY_NOT_SENT
    with pytest.raises(ValueError, match="not connected"):
        outbound.send_parked(db_session, s, "tester")
    assert not wabs_mode


# ------------------------------------------------- live links, auto mode
def test_expired_link_is_replaced_at_send_time(db_session, wabs_mode, monkeypatch):
    from datetime import timedelta
    from app.models import PortalToken
    from app.portal_tokens import issue_upload_link
    csp, m = _draft(db_session)
    old = issue_upload_link(db_session, csp, ["AGREEMENT"])
    db_session.get(PortalToken, old.split("token=")[1]).expires_at = outbound._now() - timedelta(days=1)
    m.payload_json = {"body": f"नमस्ते\nयहाँ अपलोड करें: {old}\nUpload here: {old}", "link": old}
    _allow_more(db_session, monkeypatch)
    outbound.approve(db_session, m, "tester")
    outbound.send(db_session, m)
    new = m.payload_json["link"]
    assert new != old and old not in m.payload_json["body"] and m.payload_json["body"].count(new) == 2
    assert wabs_mode[0][1][0]["link"] == new and m.status == OutboundStatus.SENT


def test_auto_mode_sends_within_limits_and_hours(db_session, wabs_mode, monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(outbound, "OUTBOUND_COMMUNICATION_MODE", "auto")
    monkeypatch.setattr(outbound, "AUTO_SEND_CHANNELS", ["WHATSAPP"])
    monkeypatch.setattr(outbound, "AUTO_SEND_HOURS", (10, 18))
    db_session.query(OutboundMessage).filter(OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW).update(
        {"status": OutboundStatus.REJECTED})                          # only this test's drafts
    onboard = [_draft(db_session)[1] for _ in range(3)]
    _, expired = _draft(db_session, template="UPLOAD_EXPIRED")
    _allow_more(db_session, monkeypatch, 2)
    assert outbound.auto_approve(db_session, datetime(2026, 10, 7, 22)) == {"outside_hours": True}
    assert all(m.status == OutboundStatus.QUEUED_FOR_REVIEW for m in onboard)
    r = outbound.auto_approve(db_session, datetime(2026, 10, 7, 11))
    assert r["WHATSAPP"] == 2 and r["WHATSAPP:limit_reached"] == 1      # the daily limit holds
    assert [m.status for m in onboard].count(OutboundStatus.APPROVED) == 2
    assert expired.status == OutboundStatus.QUEUED_FOR_REVIEW          # no Meta layout: left for a person


def test_review_mode_never_auto_approves(db_session, wabs_mode, monkeypatch):
    monkeypatch.setattr(outbound, "OUTBOUND_COMMUNICATION_MODE", "review")
    _, m = _draft(db_session)
    assert outbound.auto_approve(db_session) == {} and m.status == OutboundStatus.QUEUED_FOR_REVIEW
