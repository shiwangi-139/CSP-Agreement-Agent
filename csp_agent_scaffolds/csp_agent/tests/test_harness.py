"""The advanced harness and feedback loop: rule health and the circuit
breaker with the release check (app/rule_health.py), the send-time check and
pause (app/comms/send_gate.py), the daily reference batch
(app/active_learning.py), Coach findings followed until solved
(app/finding_tracker.py) and the shadow test's comparison
(scripts/shadow_test.py). Dates in 2999 keep these rows apart from the rest
of the shared test database."""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app import active_learning, config, finding_tracker, rule_health
from app.comms import send_gate
from app.models import (CSP, CoachFinding, Document, DocumentStatus, IngestState, ManualReviewQueue, MonitorRun,
                        OutboundMessage, OutboundStatus, OutreachCycle, ReviewStatus, RuleEvent, RuleHealth)
from scripts.shadow_test import compare
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

NOW = datetime(2999, 1, 31, 12)


@pytest.fixture()
def clean(db_session):
    db = db_session

    def wipe():
        db.rollback()
        db.query(RuleHealth).delete()
        db.query(RuleEvent).delete()
        db.query(CoachFinding).delete()
        db.query(MonitorRun).filter(MonitorRun.started_at >= datetime(2998, 1, 1)).delete()
        db.query(IngestState).filter(IngestState.key.in_([rule_health.RELEASE_CHECK_KEY, send_gate.PAUSE_KEY])).delete()
        db.commit()
    wipe()
    yield db
    wipe()


def _csp(db):
    code = f"4H{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Harness CSP", current_code=code, lookup_code=code, phone="9876512345", is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def _eval(db, at, commit, by_rule, failures=None, failure_rules=None, verdicts=None, set_name="tuning"):
    db.add(MonitorRun(kind="reading_eval", started_at=at, finished_at=at, status="OK",
                      summary={"set": set_name, "commit": commit, "by_rule": by_rule, "failures": failures or {},
                               "failure_rules": failure_rules or {}, "verdicts": verdicts or {},
                               "by_type": {"ALL": {"score": 10 * len(failures or {})}}}))
    db.commit()


# ------------------------------------------------------------ rule health
def test_a_rule_that_accepts_wrongly_goes_on_probation_and_is_released_when_fixed(clean):
    db = clean
    _eval(db, NOW - timedelta(hours=2), "aaa", {"PVR_TOP_DATE": {"correct": 20, "WRONG ACCEPT": 2},
                                                "PVR_DIGITAL_SIGNATURE_DATE": {"correct": 90, "WRONG ACCEPT": 1},
                                                "AGREEMENT_DEFAULT_1_YEAR": {"correct": 5, "WRONG ACCEPT": 3}})
    out = rule_health.refresh(db, NOW)
    h = {r.rule: r for r in db.query(RuleHealth)}
    assert h["PVR_TOP_DATE"].state == "probation" and "2 wrong accept" in h["PVR_TOP_DATE"].state_reason
    assert h["PVR_TOP_DATE"].precision == round(20 / 22, 3)
    assert h["PVR_DIGITAL_SIGNATURE_DATE"].state == "watch"          # 1 in 91: precision over 97 %
    assert h["AGREEMENT_DEFAULT_1_YEAR"].state == "watch"            # policy: never on probation
    assert "PVR_TOP_DATE: ok -> probation" in out["changes"]
    assert rule_health.held_rule(db, ["PVR_TOP_DATE", None]).rule == "PVR_TOP_DATE"
    assert [e.event for e in db.query(RuleEvent).filter_by(rule="PVR_TOP_DATE")] == ["probation"]
    # The next night's check is clean: released by itself.
    _eval(db, NOW + timedelta(hours=20), "aaa", {"PVR_TOP_DATE": {"correct": 22}})
    rule_health.refresh(db, NOW + timedelta(days=1))
    assert db.get(RuleHealth, "PVR_TOP_DATE").state == "ok"


def test_a_person_can_release_or_hold_a_rule_and_it_sticks(clean, monkeypatch):
    db = clean
    _eval(db, NOW - timedelta(hours=2), "aaa", {"PVR_TOP_DATE": {"correct": 1, "WRONG ACCEPT": 1}})
    rule_health.refresh(db, NOW)
    monkeypatch.setattr(rule_health, "_now", lambda: NOW + timedelta(hours=1))
    rule_health.set_state(db, "PVR_TOP_DATE", "ok", "admin", "the answer was wrong")
    # The same evidence (from before the release) doesn't trip it again.
    rule_health.refresh(db, NOW + timedelta(hours=2))
    assert db.get(RuleHealth, "PVR_TOP_DATE").state == "ok"
    rule_health.set_state(db, "IIBF_DATED", "probation", "admin", "new format")
    rule_health.refresh(db, NOW)
    assert db.get(RuleHealth, "IIBF_DATED").state == "probation" and db.get(RuleHealth, "IIBF_DATED").manual
    with pytest.raises(ValueError):
        rule_health.set_state(db, "PVR_DEFAULT_1_YEAR", "probation", "admin")
    events = [e.event for e in db.query(RuleEvent).order_by(RuleEvent.id)]
    assert "released" in events and "held_by_person" in events


def test_an_update_that_breaks_documents_puts_their_rules_on_probation(clean):
    db = clean
    _eval(db, NOW - timedelta(days=1), "old1", {"AGREEMENT_THIS_DAY_OF": {"correct": 40}}, failures={"7": "wrong reject"})
    _eval(db, NOW - timedelta(hours=1), "new2", {"AGREEMENT_THIS_DAY_OF": {"correct": 39, "WRONG ACCEPT": 1}},
          failures={"9": "WRONG ACCEPT"}, failure_rules={"9": ["AGREEMENT_THIS_DAY_OF", "AGREEMENT_EXPLICIT_3_YEAR"]})
    out = rule_health.refresh(db, NOW)
    rc = db.get(IngestState, rule_health.RELEASE_CHECK_KEY).value
    assert rc["verdict"] == "worse" and rc["broken_wrong_accepts"] == ["9"] and rc["fixed"] == 1
    assert out["release_check"] == "worse"
    h = db.get(RuleHealth, "AGREEMENT_THIS_DAY_OF")
    assert h.state == "probation" and "update new2" in h.state_reason
    assert db.get(RuleHealth, "AGREEMENT_EXPLICIT_3_YEAR").state == "probation"
    titles = [f["title"] for f in finding_tracker.harness_findings(db, NOW)]
    assert any(t.startswith("Update new2 made reading worse") for t in titles)
    assert any(t.startswith("Rule on probation") for t in titles)


def test_switching_the_breaker_off_only_watches(clean, monkeypatch):
    db = clean
    monkeypatch.setattr(config, "RULE_BREAKER", "watch")
    _eval(db, NOW - timedelta(hours=2), "aaa", {"PVR_TOP_DATE": {"correct": 1, "WRONG ACCEPT": 1}})
    rule_health.refresh(db, NOW)
    assert db.get(RuleHealth, "PVR_TOP_DATE").state == "watch"


# ------------------------------------------------------------ send-time check
def _msg(db, c, template="UPLOAD_MISSING", **kw):
    m = OutboundMessage(csp_id=c.id, template_name=template, channel="WHATSAPP", destination=c.phone,
                        payload_json={}, status=OutboundStatus.APPROVED, idempotency_key=uuid.uuid4().hex,
                        recipient_role="CSP", attempts=0, **kw)
    db.add(m)
    db.flush()
    return m


def test_pause_stops_everything_until_resumed(clean):
    db = clean
    c = _csp(db)
    m = _msg(db, c, "UPLOAD_RECEIPT")
    send_gate.set_pause(db, True, "admin", "wrong template")
    action, reason, until = send_gate.decide(db, m, NOW)
    assert action == "wait" and "paused by admin: wrong template" in reason and until == NOW + timedelta(minutes=15)
    assert send_gate.hold_back(db, m, NOW) and m.status == OutboundStatus.APPROVED and m.next_retry_at == until
    assert any(f["key"] == "H:paused" for f in finding_tracker.harness_findings(db, NOW))
    send_gate.set_pause(db, False, "admin")
    assert send_gate.decide(db, m, NOW)[0] == "send"


def test_quiet_hours_hold_reminders_but_not_replies(clean, monkeypatch):
    db = clean
    monkeypatch.setattr(config, "QUIET_HOURS", (21, 8))
    c = _csp(db)
    night = datetime(2999, 1, 10, 17, 30)          # 23:00 in India
    action, _, until = send_gate.decide(db, _msg(db, c, "RENEWAL_NOTICE"), night)
    assert action == "wait" and until == datetime(2999, 1, 11, 2, 30)     # 08:00 in India
    assert send_gate.decide(db, _msg(db, c, "UPLOAD_RECEIPT"), night)[0] == "send"
    assert send_gate.quiet_until(datetime(2999, 1, 10, 6, 30)) is None    # 12:00 in India


def test_a_reminder_no_longer_needed_or_repeated_is_cancelled(clean):
    db = clean
    c = _csp(db)
    issue = date.today() - timedelta(days=10)
    for t in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE"):
        db.add(Document(csp_id=c.id, document_type=t, status=DocumentStatus.VALID, readability="READABLE",
                        sha256=uuid.uuid4().hex, issue_date=issue, expiry_date=issue + timedelta(days=700), is_current=True))
    db.flush()
    m = _msg(db, c, "UPLOAD_MISSING", document_type="AGREEMENT")
    assert send_gate.hold_back(db, m, NOW)
    assert m.status == OutboundStatus.REJECTED and m.reviewed_by == send_gate.BY and "needs no document" in m.error_log
    cycle = OutreachCycle(csp_id=c.id, document_type="AGREEMENT", kind="RENEWAL", ladder="x", anchor_date=issue,
                          closed_at=datetime.now(timezone.utc).replace(tzinfo=None), close_reason="renewed")
    db.add(cycle)
    db.flush()
    assert send_gate.decide(db, _msg(db, c, "RENEWAL_FOLLOWUP", cycle_id=cycle.id), NOW)[:2] == \
        ("cancel", "its follow-up has closed (renewed)")
    sent = _msg(db, c, "RENEWAL_NOTICE")
    sent.status, sent.sent_at = OutboundStatus.SENT, NOW - timedelta(hours=3)
    db.flush()
    action, reason, _ = send_gate.decide(db, _msg(db, c, "RENEWAL_NOTICE"), NOW)
    assert action == "cancel" and f"(#{sent.id})" in reason
    over = send_gate.overview(db, NOW)
    assert over["cancelled_7d"] >= 1


# ------------------------------------------------------------ reference batch
def test_the_daily_batch_picks_the_most_useful_documents(clean, monkeypatch):
    db = clean
    c = _csp(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    risky = Document(csp_id=c.id, document_type="AGREEMENT", status=DocumentStatus.VALID, readability="READABLE",
                     sha256=uuid.uuid4().hex, uploaded_at=now, date_source="AGREEMENT_RARE_RULE",
                     extracted_fields={"risk": {"score": 45, "level": "check"}})
    plain = Document(csp_id=c.id, document_type="AGREEMENT", status=DocumentStatus.VALID, readability="READABLE",
                     sha256=uuid.uuid4().hex, uploaded_at=now, date_source="AGREEMENT_RARE_RULE")
    db.add_all([risky, plain])
    db.flush()
    ranked = [d.id for _, d, _ in active_learning.candidates(db, now)]
    assert ranked.index(risky.id) < ranked.index(plain.id)
    monkeypatch.setattr(active_learning, "candidates", lambda db, now=None: [(75, risky, ["risk 45"]), (30, plain, ["x"])])
    out = active_learning.daily_batch(db, n=1)
    assert out == {"added": 1, "documents": [risky.id]}
    q = db.query(ManualReviewQueue).filter_by(document_id=risky.id).one()
    assert q.reason.startswith("Reference check: picked because risk 45")
    db.query(ManualReviewQueue).filter_by(document_id=risky.id).delete()
    db.query(Document).filter(Document.id.in_([risky.id, plain.id])).delete()
    db.query(CSP).filter_by(id=c.id).delete()
    db.commit()
    assert active_learning.daily_batch(db, n=0)["added"] == 0


# ------------------------------------------------------------ findings followed
def test_a_finding_is_followed_until_the_fix_is_verified_and_reopens_when_it_returns(clean, monkeypatch):
    db = clean
    found = [{"key": "S:# impossible reading(s)", "level": "critical", "title": "3 impossible reading(s)"}]
    monkeypatch.setattr("app.signals.findings", lambda db, now=None: list(found))
    monkeypatch.setattr(finding_tracker, "harness_findings", lambda db, now=None: [])
    finding_tracker.track(db, NOW)
    r = db.get(CoachFinding, "S:# impossible reading(s)")
    assert r.status == "open"
    finding_tracker.mark_fixed(db, r.key, "admin", "new rule in abc1234")
    assert r.status == "fixing"
    found.clear()
    out = finding_tracker.track(db, NOW + timedelta(days=1))
    assert db.get(CoachFinding, r.key).status == "verified" and out["verified"] == 1
    found.append({"key": "S:# impossible reading(s)", "level": "critical", "title": "1 impossible reading(s)"})
    finding_tracker.track(db, NOW + timedelta(days=2))
    r = db.get(CoachFinding, r.key)
    assert r.status == "reopened" and [h["status"] for h in r.history] == ["open", "fixing", "verified", "reopened"]
    annotated = finding_tracker.annotate(db, [dict(found[0])])
    assert annotated[0]["track"]["status"] == "reopened"


def test_a_fix_that_does_not_work_reopens_after_a_week(clean, monkeypatch):
    db = clean
    monkeypatch.setattr("app.signals.findings", lambda db, now=None: [{"key": "S:x", "level": "warn", "title": "x"}])
    monkeypatch.setattr(finding_tracker, "harness_findings", lambda db, now=None: [])
    finding_tracker.track(db, NOW)
    finding_tracker.mark_fixed(db, "S:x", "admin")
    finding_tracker.track(db, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=8))
    assert "didn't work" in db.get(CoachFinding, "S:x").history[-1]["note"]


# ------------------------------------------------------------ shadow test
def test_the_shadow_comparison_says_whether_to_put_it_live():
    live = {"verdicts": {"1": "correct", "2": "WRONG ACCEPT", "3": "wrong reject", "4": "correct"}}
    good = compare(live, {"verdicts": {"1": "correct", "2": "correct", "3": "correct", "4": "correct"}})
    assert good["fixed"] == ["2", "3"] and good["broken"] == [] and good["verdict"] == "safe to put live"
    bad = compare(live, {"verdicts": {"1": "WRONG ACCEPT", "2": "correct", "3": "wrong reject", "4": "correct"}})
    assert bad["new_wrong_accepts"] == ["1"] and bad["verdict"].startswith("do not put live")


# ------------------------------------------------------------ dashboard API
def test_rules_tab_and_buttons_are_admins_only(client, monkeypatch, clean):
    from app import auth
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, _ = client
    h = {"X-API-Key": "k"}
    assert c.get("/api/hub/monitor/rules").status_code in (401, 403)
    r = c.get("/api/hub/monitor/rules", headers=h)
    assert r.status_code == 200 and {"rules", "sending", "tracked", "risk_7d", "status"} <= set(r.json())
    assert c.post("/api/hub/sending/pause", json={"on": True, "reason": "test"}, headers=h).json()["on"] is True
    assert c.get("/api/hub/monitor/rules", headers=h).json()["sending"]["pause"]["on"] is True
    assert c.post("/api/hub/sending/pause", json={"on": False}, headers=h).json()["on"] is False
    assert c.post("/api/hub/rules/PVR_TOP_DATE/state", json={"state": "probation", "note": "t"}, headers=h).json()["state"] == "probation"
    assert c.post("/api/hub/rules/PVR_DEFAULT_1_YEAR/state", json={"state": "probation"}, headers=h).status_code == 422
    assert c.post("/api/hub/findings/fixed", json={"key": "nope"}, headers=h).status_code == 422
