"""Messaging analytics (app/analytics.py): sent per channel and day, who
sent them, and what CSPs did afterwards; the test CSP is never counted."""
import uuid
from datetime import timedelta

from app import analytics
from app.comms.outbound import _now
from app.config import local_now
from app.models import AgreementEvent, CSP, CspQuestion, OutboundMessage, OutboundStatus


def _csp(db, state=None):
    code = f"6A{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="An " + code, current_code=code, phone="9876522222", is_active_in_calling_sheet=True, state=state)
    db.add(c)
    db.flush()
    return c


def _sent(db, csp, channel, who="Shiwangi", hours_ago=2, template="ONBOARD_ALL", delivery=None):
    m = OutboundMessage(csp_id=csp.id, channel=channel, recipient_role="CSP", template_name=template,
                        destination="9876522222", status=OutboundStatus.SENT, attempts=1,
                        idempotency_key=uuid.uuid4().hex, created_at=_now() - timedelta(hours=hours_ago + 1),
                        sent_at=_now() - timedelta(hours=hours_ago), reviewed_by=who, delivery_status=delivery)
    db.add(m)
    db.flush()
    return m


def test_sent_and_what_csps_did_afterwards(db_session):
    db = db_session
    a, b, test = _csp(db), _csp(db), _csp(db, state="TEST")
    _sent(db, a, "WHATSAPP", delivery="READ")
    _sent(db, a, "EMAIL")
    _sent(db, b, "WHATSAPP", who="agent (auto mode)")
    _sent(db, test, "WHATSAPP", who="test_csp script")
    now = local_now()
    db.add(AgreementEvent(csp_id=a.id, event_type="LINK_OPENED", sent_at=now - timedelta(minutes=30)))
    db.add(AgreementEvent(csp_id=a.id, event_type="PORTAL_UPLOAD", sent_at=now - timedelta(minutes=20),
                          payload={"results": [{"section": "pvr", "ok": True}]}))
    db.add(CspQuestion(csp_id=b.id, category="OTHER", text="kab tak?", status="OPEN", created_at=now - timedelta(minutes=5)))
    db.add(AgreementEvent(csp_id=b.id, event_type="LINK_OPENED", sent_at=now - timedelta(days=3)))   # before the message
    db.flush()

    ids = {a.id, b.id, test.id}
    r = analytics.build(db, ids, days=3)
    sent_day = (_now() - timedelta(hours=2) + analytics.IST).date().isoformat()
    day = next(d for d in r["days"] if d["date"] == sent_day)
    assert day["whatsapp"]["sent"] == 2 and day["email"]["sent"] == 1          # test CSP not counted
    assert day["whatsapp"]["read"] == 1 and day["whatsapp"]["delivered"] == 1
    resp = day["responses"]
    assert resp["reached"] == 2 and resp["opened"] == 1 and resp["submitted"] == 1 and resp["accepted"] == 1
    assert resp["asked"] == 1 and resp["any"] == 2                              # b's old link-open doesn't count
    who = {s["who"]: s for s in r["senders"]}
    assert who["Approved by Shiwangi"] == {"who": "Approved by Shiwangi", "whatsapp": 1, "email": 1}
    assert who["Agent (auto mode)"]["whatsapp"] == 1 and "Test (test CSP)" not in who
    assert r["periods"]["last_7_days"]["csps_reached"] == 2

    only_b = analytics.build(db, {b.id}, days=3)                                 # an RM sees their own CSPs
    assert only_b["periods"]["last_7_days"]["csps_reached"] == 1


def test_parked_by_default_page_is_off_and_nothing_is_recorded(monkeypatch):
    from app.api import portal
    monkeypatch.setattr(portal, "FEATURE_MESSAGING_ANALYTICS", False)
    calls = []

    class DB:                                    # any query would mean a record was attempted
        def query(self, *a):
            calls.append(a)
    portal._record_link_open(DB(), 1)
    assert calls == []
