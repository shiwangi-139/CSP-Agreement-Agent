"""The 45-day plan (app/policy.py PLAN_WEEKS, renewal_engine._run_plan_week,
escalation lists): one CSP message a week, firmer each week with the number
of reminders already sent; DC list from week 3, RM list from week 5; no daily
messages; the closure list after 45 days."""
import uuid
from datetime import date, datetime, timedelta

from app import escalation, policy, renewal_engine
from app.models import CSP, Document, DocumentStatus, InternalUser, OutboundMessage, OutboundStatus, OutreachCycle

START = date(2026, 11, 5)          # a Thursday


def _plan(monkeypatch):
    monkeypatch.setattr(policy, "PLAN_FROM", START)


def _csp(db, rm=None, dc=None, sent=3, anchor=date(2026, 10, 1)):
    code = f"7P{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name=f"Plan {code}", current_code=code, lookup_code=code, phone="9876501234", whatsapp_number="9876501234",
            is_active_in_calling_sheet=True, rm_id=rm.id if rm else None, dc_id=dc.id if dc else None)
    db.add(c)
    db.flush()
    db.add(Document(csp_id=c.id, document_type="AGREEMENT", status=DocumentStatus.VALID, is_current=True,
                    readability="READABLE", issue_date=date(2026, 1, 1), expiry_date=date(2029, 1, 1),
                    sha256=uuid.uuid4().hex))
    cyc = OutreachCycle(csp_id=c.id, document_type="ALL", kind="UPLOAD", ladder="UPLOAD", anchor_date=anchor,
                        csp_messages=0, rm_messages=0, dc_messages=0)
    db.add(cyc)
    for i in range(sent):
        db.add(OutboundMessage(csp_id=c.id, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876501234",
                               status=OutboundStatus.SENT, sent_at=datetime(2026, 10, 10 + i, 9), recipient_role="CSP",
                               idempotency_key=uuid.uuid4().hex, created_at=datetime(2026, 10, 10 + i, 9)))
    db.add(OutboundMessage(csp_id=c.id, template_name="UPLOAD_RECEIPT", channel="WHATSAPP", destination="9876501234",
                           status=OutboundStatus.SENT, sent_at=datetime(2026, 10, 20, 9), recipient_role="CSP",
                           idempotency_key=uuid.uuid4().hex, created_at=datetime(2026, 10, 20, 9)))   # not a reminder
    db.flush()
    return c


def _drafts(db, c):
    return (db.query(OutboundMessage).filter(OutboundMessage.csp_id == c.id, OutboundMessage.stage.like("P-W%"))
            .order_by(OutboundMessage.id).all())


def test_weeks_from_day_0_and_a_head_start_for_csps_already_pending(monkeypatch):
    _plan(monkeypatch)
    old, new = date(2026, 9, 1), date(2026, 11, 20)
    assert policy.plan_week(old, START) == 2 and policy.plan_week(old, START + timedelta(days=7)) == 3
    assert policy.plan_week(old, START + timedelta(days=34)) == 6 and policy.plan_week(old, START + timedelta(days=35)) == 7
    assert policy.plan_week(old, START + timedelta(days=38)) == "closure"           # day 45
    assert policy.plan_week(new, new) == 1 and policy.plan_week(new, new + timedelta(days=42)) == 7
    assert not policy.in_daily_push(START)                                       # weekly, never daily
    assert policy.plan_list_day(START + timedelta(days=7)) and not policy.plan_list_day(START + timedelta(days=8))


def test_one_message_a_week_firmer_each_week_with_the_count(db_session, monkeypatch):
    _plan(monkeypatch)
    db = db_session
    c = _csp(db)
    renewal_engine.run_for_csp(db, c, START)
    renewal_engine.run_for_csp(db, c, START + timedelta(days=3))                  # same week: nothing new
    d = _drafts(db, c)
    assert [m.template_name for m in d] == ["PLAN_REMINDED"] * 2                    # WhatsApp + email
    body = d[0].payload_json["body"]
    assert "3 बार" in body and "reminded you 3 times" in body and "upload?token=" in body
    renewal_engine.run_for_csp(db, c, START + timedelta(days=7))                  # week 3: unsent week 2 replaced
    assert {m.template_name for m in _drafts(db, c) if m.status == OutboundStatus.QUEUED_FOR_REVIEW} == {"PLAN_WARNING"}
    assert all(m.status in (OutboundStatus.REJECTED, OutboundStatus.BLOCKED)      # email: no address on file
               for m in _drafts(db, c) if m.template_name == "PLAN_REMINDED")
    renewal_engine.run_for_csp(db, c, START + timedelta(days=21))                 # week 5: 3 days
    soon = [m for m in _drafts(db, c) if m.template_name == "PLAN_CLOSING_SOON"][0]
    assert (START + timedelta(days=24)).strftime("%d-%m-%Y") in soon.payload_json["body"]
    renewal_engine.run_for_csp(db, c, START + timedelta(days=28))
    renewal_engine.run_for_csp(db, c, START + timedelta(days=35))
    assert [m.stage for m in _drafts(db, c) if m.channel == "WHATSAPP"][-2:] == ["P-W6", "P-W7"]
    n = len(_drafts(db, c))
    renewal_engine.run_for_csp(db, c, START + timedelta(days=38))                 # day 45: closure list, no message
    assert len(_drafts(db, c)) == n


def test_no_reminder_sent_yet_gets_the_usual_message(db_session, monkeypatch):
    _plan(monkeypatch)
    db = db_session
    c = _csp(db, sent=0)
    renewal_engine.run_for_csp(db, c, START)
    assert {m.template_name for m in _drafts(db, c)} == {"UPLOAD_MISSING"}


def test_dc_list_from_week_3_and_rm_list_from_week_5(db_session, monkeypatch):
    _plan(monkeypatch)
    db = db_session
    tag = uuid.uuid4().hex[:5]
    rm = InternalUser(name=f"RM {tag}", role="RM", phone="9000000177")
    dc = InternalUser(name=f"DC {tag}", role="DC", phone="9000000188")
    db.add_all([rm, dc])
    db.flush()
    c = _csp(db, rm, dc)
    assert escalation.draft_nudges(db, START) == {"skipped": "not a nudge day"}
    assert escalation.draft_staff_lists(db, "DC", START + timedelta(days=1)) == {"skipped": "no DC list today"}
    escalation.draft_staff_lists(db, "DC", START)                                 # week 2: not on the list yet
    assert not db.query(OutboundMessage).filter(OutboundMessage.idempotency_key.like(f"STAFF:DC:{dc.id}:%")).count()
    week3 = START + timedelta(days=7)
    escalation.draft_staff_lists(db, "DC", week3)
    escalation.draft_staff_lists(db, "RM", week3)
    m = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == f"STAFF:DC:{dc.id}:{week3:%Y%m%d}").one()
    assert m.payload_json["layout"] == "plan_list" and f"{c.current_code} {c.name} (P, I · 3 msgs)" in m.payload_json["layout_values"]["codes"]
    assert not db.query(OutboundMessage).filter(OutboundMessage.idempotency_key.like(f"STAFF:RM:{rm.id}:%")).count()
    week5 = START + timedelta(days=21)
    escalation.draft_staff_lists(db, "RM", week5)
    r = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == f"STAFF:RM:{rm.id}:{week5:%Y%m%d}").one()
    assert f"DC {dc.name}" in r.payload_json["layout_values"]["codes"]


def test_whatsapp_layout_values_for_the_plan(db_session, monkeypatch):
    from app.comms import wabs
    from app.compliance import evaluate
    _plan(monkeypatch)
    db = db_session
    c = _csp(db)
    v = wabs.layout_values("PLAN_CLOSING_SOON", c, evaluate(db, c), "")
    assert v["count"] == "3" and "IIBF" in v["doc_en"] and v["date"]
    assert set(wabs.CLEAN_MAP["plan_soon"].values()) <= {"name", "code", "link", *v}
