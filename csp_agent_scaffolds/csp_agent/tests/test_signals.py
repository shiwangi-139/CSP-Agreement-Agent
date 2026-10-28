"""app/signals.py: the agent's own warning signs are found in its records,
recorded once, and nothing else changes. Dates are in 2998/2999 so only
this file's rows fall in the window (the test database is shared)."""
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import signals
from app.models import (AgentSignal, AgreementEvent, CSP, CspQuestion, Document, DocumentStatus, InboundMessage,
                        ManualReviewQueue, ReviewStatus)
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

NOW = datetime(2999, 1, 31, 12)
T = datetime(2999, 1, 20, 12)


@pytest.fixture()
def made(db_session):
    """Rows made by a test, removed afterwards."""
    rows = []
    yield rows
    db = db_session
    db.rollback()
    ids = [r.id for r in rows if isinstance(r, Document)]
    db.query(AgentSignal).filter(AgentSignal.happened_at >= datetime(2998, 1, 1)).delete()
    db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id.in_(ids)).delete()
    for r in reversed(rows):
        if not isinstance(r, ManualReviewQueue):
            db.query(type(r)).filter(type(r).id == r.id).delete()
    db.commit()


def _csp(db, made, state="Bihar"):
    code = f"9S{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Signal CSP", current_code=code, lookup_code=code, phone="9876543210", state=state,
            is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    made.append(c)
    return c


def _doc(db, made, c, doc_type="AGREEMENT", status=DocumentStatus.VALID, at=T, **kw):
    d = Document(csp_id=c.id, document_type=doc_type, status=status, sha256=uuid.uuid4().hex,
                 readability=kw.pop("readability", "READABLE"), uploaded_at=at,
                 upload_channel=kw.pop("channel", "CSP_UPLOAD_PORTAL"), **kw)
    db.add(d)
    db.flush()
    made.append(d)
    return d


def _queue(db, made, d, reason, status=ReviewStatus.PENDING, at=T):
    q = ManualReviewQueue(document_id=d.id, reason=reason, status=status, created_at=at,
                          resolved_at=at if status != ReviewStatus.PENDING else None)
    db.add(q)
    db.flush()
    made.append(q)
    return q


def _keys(db, kind):
    return {s.key for s in db.query(AgentSignal).filter(AgentSignal.kind == kind,
                                                        AgentSignal.happened_at >= datetime(2998, 1, 1))}


def test_refused_then_accepted_and_the_same_file_again(db_session, made):
    db = db_session
    c = _csp(db, made)
    refused = _doc(db, made, c, "POLICE_VERIFICATION", DocumentStatus.UNREADABLE, T - timedelta(days=5), readability="UNREADABLE")
    by_person = _doc(db, made, c, "POLICE_VERIFICATION", DocumentStatus.REJECTED, T - timedelta(days=4))
    _queue(db, made, by_person, "Type not confirmed", ReviewStatus.REJECTED, T - timedelta(days=4))
    _doc(db, made, c, "POLICE_VERIFICATION", DocumentStatus.VALID, T, issue_date=date(2998, 12, 1), expiry_date=date(2999, 12, 1))
    m = InboundMessage(external_message_id=uuid.uuid4().hex, sender="x@y.z", received_at=T,
                       attachment_decisions=[{"filename": "pvr.pdf", "decision": "DUPLICATE", "document_id": refused.id}])
    e = AgreementEvent(csp_id=c.id, event_type="PORTAL_UPLOAD", source="CSP_UPLOAD_PORTAL", sent_at=T,
                       payload={"results": [{"section": "pvr", "ok": False, "reason": "dup_unreadable", "document_id": refused.id}]})
    db.add_all([m, e])
    db.flush()
    made += [m, e]
    db.commit()
    signals.collect(db, now=NOW)
    assert str(refused.id) in _keys(db, "S1_REFUSED_THEN_ACCEPTED")
    assert str(by_person.id) not in _keys(db, "S1_REFUSED_THEN_ACCEPTED")     # a person decided: not the agent's error
    assert _keys(db, "S2_SAME_FILE_AGAIN") == {f"mail:{m.id}:{refused.id}", f"upload:{e.id}:{refused.id}"}


def test_disagreeing_impossible_and_guessed_readings(db_session, made):
    db = db_session
    c = _csp(db, made)
    a = _doc(db, made, c, "POLICE_VERIFICATION", issue_date=date(2998, 6, 1), expiry_date=date(2999, 6, 1), at=T - timedelta(days=40))
    b = _doc(db, made, c, "POLICE_VERIFICATION", issue_date=date(2998, 6, 1), expiry_date=date(2998, 12, 1))
    bad = _doc(db, made, c, issue_date=date(2999, 1, 10), expiry_date=date(2998, 1, 10))
    future = _doc(db, made, c, issue_date=date(2999, 3, 1), expiry_date=date(3002, 3, 1))
    iibf = _doc(db, made, c, "IIBF_CERTIFICATE", issue_date=date(2998, 1, 1))
    fine = _doc(db, made, c, issue_date=date(2999, 1, 1), expiry_date=date(3002, 1, 1), validity_rule_used="AGREEMENT_EXPLICIT_3_YEAR")
    hidden = _doc(db, made, c, issue_date=date(2999, 1, 1), expiry_date=date(3000, 1, 1), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR",
                  extracted_fields={"why": {"lines": [{"text": "shall r*main valid for a from the Effective Date.) On this day of"}]}})
    plain = _doc(db, made, c, issue_date=date(2999, 1, 2), expiry_date=date(3000, 1, 2), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR",
                 extracted_fields={"why": {"lines": [{"text": "On this day of, 02/01/2999 CSP Name X"}]}})
    db.commit()
    signals.collect(db, now=NOW)
    assert f"{a.id}-{b.id}" in _keys(db, "S3_READINGS_DISAGREE")
    s4 = _keys(db, "S4_IMPOSSIBLE_RESULT")
    assert {f"{bad.id}:expiry_before_issue", f"{future.id}:issue_after_arrival", f"{iibf.id}:iibf_no_number"} <= s4
    assert not any(k.startswith(f"{fine.id}:") for k in s4)
    assert _keys(db, "S5_GUESSED_VALIDITY_WORDS") == {str(hidden.id)}
    assert str(plain.id) not in _keys(db, "S5_GUESSED_VALIDITY_WORDS")


def test_corrections_questions_upload_failures_and_review_causes(db_session, made):
    db = db_session
    c = _csp(db, made)
    d = _doc(db, made, c)
    corrected = _queue(db, made, d, "Validity check: no term or end date was read", ReviewStatus.CORRECTED)
    spot_rejected = _queue(db, made, d, "Spot check: confirm the document type", ReviewStatus.REJECTED)
    plain_rejected = _queue(db, made, d, "Issue date could not be read (blank)", ReviewStatus.REJECTED)
    ai = _queue(db, made, d, "Dates read by the AI model (2999-01-01 -> 3000-01-01): check them")
    unread = _doc(db, made, c, "POLICE_VERIFICATION", DocumentStatus.UNREADABLE, T - timedelta(days=2), readability="UNREADABLE",
                  extracted_fields={"readability_reason": "Too blurry to read."})
    q = CspQuestion(csp_id=c.id, category="UPLOAD_PROBLEM", text="upload nahi ho raha", created_at=T)
    events = [AgreementEvent(csp_id=c.id, event_type="PORTAL_UPLOAD", sent_at=T - timedelta(days=i),
                             payload={"results": [{"section": "agreement", "ok": False, "reason": "wrong_doc"}]}) for i in range(3)]
    db.add_all([q, *events])
    db.flush()
    made += [q, *events]
    db.commit()
    signals.collect(db, now=NOW)
    assert _keys(db, "S6_PERSON_CORRECTED") == {f"q:{corrected.id}", f"q:{spot_rejected.id}"}
    assert _keys(db, "S7_QUESTION_AFTER_REFUSAL") == {str(q.id)}
    assert len([k for k in _keys(db, "S8_REPEATED_UPLOAD_FAILURES") if k.startswith(f"{c.id}:wrong_doc")]) == 1
    causes = {s.key: s.facts["cause"] for s in db.query(AgentSignal).filter(AgentSignal.kind == "S10_REVIEW_CAUSE",
                                                                            AgentSignal.happened_at >= datetime(2998, 1, 1))}
    assert causes[f"q:{spot_rejected.id}"] == "deliberate check"
    assert causes[f"q:{ai.id}"] == "dates only from the AI model"
    assert causes[f"q:{plain_rejected.id}"] == "issue date not found"
    assert causes[f"q:{corrected.id}"] == "validity guessed"
    assert causes[f"d:{unread.id}"] == "file not readable"
    o = signals.overview(db, now=NOW)
    by = {x["cause"]: x for x in o["causes"]["by_cause"]}
    assert not by["deliberate check"]["fixable"] and by["file not readable"]["examples"][0]["csp_id"] == c.id
    assert o["causes"]["total"] == 5 and o["causes"]["fixable"] == 4


def test_running_again_adds_nothing_changes_nothing_else_and_old_signs_go(db_session, made):
    db = db_session
    c = _csp(db, made)
    _doc(db, made, c, issue_date=date(2999, 1, 10), expiry_date=date(2998, 1, 10))
    db.commit()
    before = (db.query(Document).count(), db.query(ManualReviewQueue).count(), db.query(CSP).count())
    first = signals.collect(db, now=NOW)
    assert sum(first["new"].values()) >= 1
    assert sum(signals.collect(db, now=NOW)["new"].values()) == 0
    assert (db.query(Document).count(), db.query(ManualReviewQueue).count(), db.query(CSP).count()) == before
    old = AgentSignal(kind="S4_IMPOSSIBLE_RESULT", key="old", strength="strong", happened_at=datetime(2998, 5, 1),
                      first_seen=datetime(2998, 5, 1), last_seen=datetime(2998, 5, 1))
    db.add(old)
    db.commit()
    assert signals.collect(db, now=NOW)["removed"] >= 1
    assert "old" not in _keys(db, "S4_IMPOSSIBLE_RESULT")


def test_a_rule_that_suddenly_takes_over_is_drift(db_session, made):
    db = db_session
    c = _csp(db, made)
    now = datetime(2998, 6, 30, 12)
    for i in range(20):
        _doc(db, made, c, at=now - timedelta(days=10 + i), validity_rule_used="AGREEMENT_RANGE_FROM_TO")
    for i in range(12):
        _doc(db, made, c, at=now - timedelta(days=1, hours=i), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR")
    db.commit()
    signals.collect(db, now=now)
    keys = _keys(db, "S9_RULE_DRIFT")
    assert any(k.startswith("validity rule:AGREEMENT_DEFAULT_1_YEAR:") for k in keys)
    assert not any("AGREEMENT_RANGE_FROM_TO" in k for k in keys)        # 20 over 4 weeks: too few a week to judge


def test_signals_tab_and_excel_are_admins_only(client, monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, _ = client
    hdr = {"X-API-Key": "k"}
    d = c.get("/api/hub/monitor/signals", headers=hdr).json()
    assert {k["kind"] for k in d["kinds"]} == set(signals.KINDS) and "causes" in d
    r = c.get("/api/hub/signals.xlsx", headers=hdr)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.get("/api/hub/signals.xlsx").status_code in (401, 403)
