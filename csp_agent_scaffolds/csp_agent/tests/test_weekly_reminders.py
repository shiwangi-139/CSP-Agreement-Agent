"""One reminder to a CSP in any 7 days, whatever its kind and follow-up; the
plan's clock carries on when a follow-up opens again; every CSP message
carries the common link (decided 11-11-2026)."""
import uuid
from datetime import datetime, timedelta

from app import policy, renewal_engine
from app.comms import outbound, send_gate
from app.models import OutboundMessage, OutboundStatus, OutreachCycle
from tests.test_plan_45_days import START, _csp, _drafts, _plan


def _sent(db, c, days_ago, template="PLAN_REMINDED", stage="P-W1"):
    when = datetime.combine(START, datetime.min.time()) + timedelta(hours=9) - timedelta(days=days_ago)
    m = OutboundMessage(csp_id=c.id, template_name=template, channel="WHATSAPP", destination="9876501234",
                        status=OutboundStatus.SENT, sent_at=when, recipient_role="CSP", stage=stage,
                        idempotency_key=uuid.uuid4().hex, created_at=when)
    db.add(m)
    db.flush()
    return m


def _approved(db, c, template="UPLOAD_MISSING", stage="U-2", dest=None):
    # A number of its own: other tests' messages to 9876501234 must not count as a repeat.
    m = OutboundMessage(csp_id=c.id, template_name=template, channel="WHATSAPP", destination=dest or f"97{uuid.uuid4().int % 10**8:08d}",
                        status=OutboundStatus.APPROVED, recipient_role="CSP", stage=stage, payload_json={"body": "x"},
                        idempotency_key=uuid.uuid4().hex, created_at=datetime.now())
    db.add(m)
    db.flush()
    return m


def test_no_second_reminder_within_7_days_at_send_time(db_session, monkeypatch):
    db = db_session
    monkeypatch.setattr(send_gate, "quiet_until", lambda now: None)
    c = _csp(db, sent=0)
    _sent(db, c, days_ago=0)
    now = datetime.combine(START, datetime.min.time()) + timedelta(days=3, hours=10)
    action, reason, _ = send_gate.decide(db, _approved(db, c), now)
    assert action == "cancel" and "one reminder a week" in reason
    assert send_gate.decide(db, _approved(db, c, stage="MANUAL"), now)[0] == "send"            # a person sent it
    assert send_gate.decide(db, _approved(db, c, template="UPLOAD_RECEIPT", stage="R"), now)[0] == "send"
    later = now + timedelta(days=5)
    assert send_gate.decide(db, _approved(db, c, stage="U-3"), later)[0] == "send"             # 8 days on


def test_this_weeks_message_waits_for_the_7_days(db_session, monkeypatch):
    _plan(monkeypatch)
    db = db_session
    c = _csp(db)                                   # 3 old reminders in October
    _sent(db, c, days_ago=2, template="UPLOAD_EXPIRED", stage="T-3")   # another follow-up, 2 days ago
    renewal_engine.run_for_csp(db, c, START)
    assert _drafts(db, c) == []                     # nothing drafted inside the 7 days
    renewal_engine.run_for_csp(db, c, START + timedelta(days=5))
    assert [m.template_name for m in _drafts(db, c)][:1] == ["PLAN_REMINDED"]


def test_the_plan_goes_on_from_the_same_week_after_a_follow_up_reopens(db_session, monkeypatch):
    _plan(monkeypatch)
    db = db_session
    c = _csp(db)
    old = db.query(OutreachCycle).filter_by(csp_id=c.id).one()
    old.closed_at, old.close_reason = datetime.combine(START + timedelta(days=10), datetime.min.time()), "ALL_DOCUMENTS_RECEIVED"
    db.flush()
    today = START + timedelta(days=14)               # week 4 of the plan for this CSP
    renewal_engine.run_for_csp(db, c, today)
    new = db.query(OutreachCycle).filter(OutreachCycle.csp_id == c.id, OutreachCycle.closed_at.is_(None)).one()
    assert new.anchor_date == old.anchor_date       # not a fresh week 1
    assert {m.template_name for m in _drafts(db, c)} == {policy.PLAN_WEEKS[4]["template"]}


def test_csp_messages_wait_while_the_common_link_is_off(db_session, monkeypatch):
    from app import csp_common_link
    db = db_session
    c = _csp(db, sent=0)
    m = _approved(db, c, dest=c.whatsapp_number)          # the CSP's number on record
    m.payload_json = {"body": "Upload here: https://x/upload?token=abc", "link": "https://x/upload?token=abc"}
    monkeypatch.setattr(csp_common_link, "current_url", lambda db: None)
    monkeypatch.setattr(send_gate, "hold_back", lambda db, msg: False)
    outbound.send(db, m)
    assert m.status == OutboundStatus.APPROVED and "common link is switched off" in m.error_log and m.next_retry_at
