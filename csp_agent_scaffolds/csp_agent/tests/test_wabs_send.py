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
    assert contacts[0]["code"] == csp.current_code and contacts[0]["link"] == "https://x/upload?token=t"


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
