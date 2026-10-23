"""The document campaign: evening nudge to CSPs and RM / DC lists."""
import uuid
from datetime import date, datetime, timedelta

from app import escalation, policy
from app.comms import outbound
from app.models import CSP, Document, DocumentStatus, InternalUser, OutboundMessage, OutboundStatus

DAY = date(2026, 10, 31)


def _on(monkeypatch):
    monkeypatch.setattr(policy, "CAMPAIGN_FROM", date(2026, 10, 23))
    monkeypatch.setattr(policy, "ESCALATE_FROM", date(2026, 10, 30))
    monkeypatch.setattr(policy, "ESCALATE_DC_FROM", date(2026, 11, 3))
    monkeypatch.setattr(policy, "CAMPAIGN_UNTIL", date(2026, 11, 13))


def _csp(db, rm=None, dc=None, valid_all=False):
    code = f"7E{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name=f"Camp {code}", current_code=code, lookup_code=code, phone="9876501234",
            whatsapp_number="9876501234", is_active_in_calling_sheet=True,
            rm_id=rm.id if rm else None, dc_id=dc.id if dc else None)
    db.add(c)
    db.flush()
    for t in (("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE") if valid_all else ("AGREEMENT",)):
        db.add(Document(csp_id=c.id, document_type=t, status=DocumentStatus.VALID, is_current=True,
                        readability="READABLE", issue_date=date(2026, 1, 1), expiry_date=date(2029, 1, 1),
                        sha256=uuid.uuid4().hex))
    db.flush()
    return c


def _sent_link(db, c):
    db.add(OutboundMessage(csp_id=c.id, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="9876501234",
                           status=OutboundStatus.SENT, sent_at=datetime.utcnow() - timedelta(hours=6),
                           recipient_role="CSP", idempotency_key=uuid.uuid4().hex))
    db.flush()


def test_policy_dates():
    assert policy.staff_list_due("RM", date(2000, 1, 3))          # tests: campaign off -> weekly on Monday
    assert not policy.staff_list_due("RM", date(2000, 1, 4))


def test_nudge_only_to_csps_still_pending_who_got_the_link(db_session, monkeypatch):
    _on(monkeypatch)
    db = db_session
    pending, done, no_link = _csp(db), _csp(db, valid_all=True), _csp(db)
    _sent_link(db, pending)
    _sent_link(db, done)
    assert escalation.draft_nudges(db, date(2026, 10, 29)) == {"skipped": "not a nudge day"}
    escalation.draft_nudges(db, DAY)
    escalation.draft_nudges(db, DAY)                                  # once a day
    nudges = db.query(OutboundMessage).filter(OutboundMessage.template_name == "UPLOAD_NUDGE",
                                              OutboundMessage.csp_id.in_([pending.id, done.id, no_link.id])).all()
    assert [m.csp_id for m in nudges] == [pending.id]
    assert "Police Verification" in nudges[0].payload_json["body"] and "upload?token" not in nudges[0].payload_json["body"]


def test_rm_list_from_the_escalation_day_dc_list_later(db_session, monkeypatch):
    _on(monkeypatch)
    db = db_session
    tag = uuid.uuid4().hex[:5]
    rm = InternalUser(name=f"RM {tag}", role="RM", phone="9000000077")
    dc = InternalUser(name=f"DC {tag}", role="DC", phone="9000000088")
    skip = InternalUser(name="Narottam", role="DC", phone="9000000099")
    db.add_all([rm, dc, skip])
    db.flush()
    a, b = _csp(db, rm, dc), _csp(db, rm, dc)
    _csp(db, rm, skip)
    _csp(db, rm, dc, valid_all=True)                                   # complete: not listed
    assert escalation.draft_staff_lists(db, "RM", date(2026, 10, 29)) == {"skipped": "no RM list today"}
    escalation.draft_staff_lists(db, "RM", DAY)
    assert escalation.draft_staff_lists(db, "DC", DAY) == {"skipped": "no DC list today"}
    escalation.draft_staff_lists(db, "DC", date(2026, 11, 3))
    rm_msg = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == f"STAFF:RM:{rm.id}:20261031").one()
    assert rm_msg.payload_json["layout_values"]["count"] == 3 and a.current_code in rm_msg.payload_json["layout_values"]["codes"]
    assert "(P, I)" in rm_msg.payload_json["layout_values"]["codes"]
    dc_msg = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == f"STAFF:DC:{dc.id}:20261103").one()
    assert dc_msg.payload_json["layout_values"]["count"] == 2 and b.current_code in dc_msg.payload_json["layout_values"]["codes"]
    assert not db.query(OutboundMessage).filter(OutboundMessage.idempotency_key.like(f"STAFF:DC:{skip.id}:%")).count()
    # Auto mode approves it like a CSP message; it goes only to that RM's phone.
    monkeypatch.setattr(outbound, "OUTBOUND_COMMUNICATION_MODE", "auto")
    monkeypatch.setattr(outbound, "WHATSAPP_DAILY_LIMIT", 10_000)
    outbound.auto_approve(db, now=datetime(2026, 10, 31, 11, 5))
    assert rm_msg.status == OutboundStatus.APPROVED
    monkeypatch.setattr(outbound, "WHATSAPP_MODE", "wabs")
    sent = {}
    import app.comms.wabs as wabs
    monkeypatch.setattr(wabs, "send_clean", lambda kind, contacts, src: sent.update(kind=kind, c=contacts[0]) or {"action": "sent", "job_id": 7})
    outbound.send(db, rm_msg)
    assert rm_msg.status == OutboundStatus.SENT and sent["kind"] == "staff_list" and sent["c"]["phone"] == "919000000077"
    rm.phone = "9111111111"                                            # number changed: the guard stops it
    db.flush()
    rm_msg.status = OutboundStatus.APPROVED
    outbound.send(db, rm_msg)
    assert rm_msg.status == OutboundStatus.BLOCKED


def test_codes_are_cut_to_fit_one_message():
    rows = [(CSP(current_code=f"1A85{i:04d}"), ["POLICE_VERIFICATION"]) for i in range(100)]
    text = escalation._codes(rows)
    assert len(text) < escalation.CODES_MAX_CHARS + 20 and text.endswith("more")
