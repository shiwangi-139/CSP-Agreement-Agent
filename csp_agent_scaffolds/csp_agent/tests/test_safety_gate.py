"""app/safety_gate.py and app/evidence.py: every new reading gets an
evidence pack and a risk score; a risky reading is held for a person (the CSP
is told our team is checking it), a medium one counts and a person checks it."""
import uuid
from datetime import date, timedelta

import pytest

from app import safety_gate
from app.compliance import evaluate
from app.document_service import store_extracted_document
from app.evidence import dates_on_document
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, RuleEvent, RuleHealth
from app.signals import review_cause
from tests.test_portal_and_api import _csp as _portal_csp, _form, _pvr_pdf, client  # noqa: F401  (fixture)

TODAY = date.today()


@pytest.fixture(autouse=True)
def _no_rules(db_session):
    """Rule health is shared by the test database: start and end clean."""
    def clean():
        db_session.query(RuleHealth).delete()
        db_session.query(RuleEvent).delete()
        db_session.commit()
    clean()
    yield
    db_session.rollback()
    clean()


def _csp(db):
    code = f"5G{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Gate CSP", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def _store(db, csp, issue, expiry, doc_type="AGREEMENT", text="", **kw):
    ex = {"readability": "READABLE", "document_type": doc_type, "start_date": issue.isoformat(),
          "expiry_date": expiry.isoformat(), "date_source": "AGREEMENT_THIS_DAY_OF",
          "validity_rule_used": "AGREEMENT_EXPLICIT_3_YEAR", "confidence": 0.95, "page_count": 13,
          "pages_readable": 13, "match_text": text, **kw}
    out = store_extracted_document(db, csp, f"%PDF-1.4 {uuid.uuid4().hex}".encode(), "x.pdf", "application/pdf", ex,
                                   channel="TEST")
    db.flush()
    return out.document


def _queue(db, d):
    return [q.reason for q in db.query(ManualReviewQueue).filter_by(document_id=d.id)]


def test_impossible_dates_are_named():
    a = TODAY
    assert [c for c, _ in safety_gate.impossible(a, a - timedelta(days=1), a)] == ["expiry_before_issue"]
    assert [c for c, _ in safety_gate.impossible(a + timedelta(days=5), a + timedelta(days=400), a)] == \
        ["issue_after_arrival"]
    assert [c for c, _ in safety_gate.impossible(a, a + timedelta(days=7 * 365), a)] == ["over_6_years"]
    assert safety_gate.impossible(a, a + timedelta(days=3 * 365), a) == []
    assert safety_gate.impossible(a + timedelta(days=1), None, a) == []    # one day early: time zones


def test_every_date_on_the_document_gets_its_kind():
    text = ("Date of Birth: 01/02/1990\nOn this day of 15.06.2025 (Effective Date)\n"
            "This agreement is valid upto 14/06/2028\nDigitally signed by OFFICER Date: 16.06.2025\n")
    found = {x["date"]: x for x in dates_on_document(text, date(2025, 6, 15), date(2028, 6, 14))}
    assert found["1990-02-01"]["kind"] == "birth"
    assert found["2025-06-15"]["kind"] == "signing" and found["2025-06-15"]["used"] == "issue"
    assert found["2028-06-14"]["kind"] == "end" and found["2028-06-14"]["used"] == "expiry"
    assert found["2025-06-16"]["kind"] == "digital_signature"


def test_a_clean_reading_is_accepted_with_its_evidence_and_low_risk(db_session):
    db = db_session
    issue = TODAY - timedelta(days=30)
    d = _store(db, _csp(db), issue, issue + timedelta(days=3 * 365),
               text=f"On this day of {issue:%d.%m.%Y} the agreement is valid for three years")
    assert d.status == DocumentStatus.VALID
    f = d.extracted_fields
    assert f["risk"]["level"] == "low" and f["risk"]["score"] < 30
    assert f["evidence"]["document"]["dates"][0]["used"] == "issue"
    assert f["evidence"]["csp"]["copies"] == 0 and f["evidence"]["rules"]["date"] == "AGREEMENT_THIS_DAY_OF"
    assert not any((r or "").startswith(("Safety check", "Risk check")) for r in _queue(db, d))


def test_impossible_readings_are_held_and_do_not_count(db_session):
    db = db_session
    c = _csp(db)
    held = _store(db, c, TODAY - timedelta(days=30), TODAY - timedelta(days=60))
    assert held.status == DocumentStatus.NEEDS_REVIEW and not held.is_current
    [reason] = _queue(db, held)
    assert reason.startswith("Safety check: risk 100: expiry") and "not told it is accepted" in reason
    assert held.extracted_fields["safety_check"] == reason and held.extracted_fields["risk"]["level"] == "hold"
    assert review_cause(reason) == "held: risky reading"
    state = evaluate(db, c)
    assert state.docs["AGREEMENT"].status == "MISSING" and state.docs["AGREEMENT"].in_review

    later = _store(db, c, TODAY + timedelta(days=20), TODAY + timedelta(days=400), doc_type="POLICE_VERIFICATION")
    assert later.status == DocumentStatus.NEEDS_REVIEW and "after it arrived" in _queue(db, later)[0]


def test_a_copy_that_disagrees_with_the_one_on_file_is_held(db_session):
    db = db_session
    c = _csp(db)
    issue = TODAY - timedelta(days=100)
    first = _store(db, c, issue, issue + timedelta(days=3 * 365))
    assert first.status == DocumentStatus.VALID
    second = _store(db, c, issue, issue + timedelta(days=365))
    assert second.status == DocumentStatus.NEEDS_REVIEW
    assert f"another copy (#{first.id})" in _queue(db, second)[0]
    # Another type with the same issue date is not a copy.
    pvr = _store(db, c, issue, issue + timedelta(days=365), doc_type="POLICE_VERIFICATION")
    assert pvr.status == DocumentStatus.VALID


def test_a_printed_end_date_the_default_missed_is_checked_by_a_person(db_session):
    db = db_session
    issue = TODAY - timedelta(days=40)
    end = issue + timedelta(days=3 * 365)
    text = f"On this day of {issue:%d.%m.%Y}. This agreement shall remain valid upto {end:%d/%m/%Y}."
    d = _store(db, _csp(db), issue, issue + timedelta(days=365), text=text,
               validity_rule_used="AGREEMENT_DEFAULT_1_YEAR", confidence=0.5)
    risk = d.extracted_fields["risk"]
    assert {r["code"] for r in risk["reasons"]} >= {"missed_end_date", "weak_reading"}
    assert risk["level"] == "check" and d.status == DocumentStatus.VALID       # counts meanwhile
    [reason] = _queue(db, d)
    assert reason.startswith("Risk check: risk 45") and "printed" in reason
    assert review_cause(reason) == "risk check (counted meanwhile)"


def test_a_rule_on_probation_holds_its_readings(db_session):
    db = db_session
    db.add(RuleHealth(rule="AGREEMENT_THIS_DAY_OF", state="probation", state_reason="2 wrong accepts"))
    db.flush()
    issue = TODAY - timedelta(days=10)
    d = _store(db, _csp(db), issue, issue + timedelta(days=3 * 365))
    assert d.status == DocumentStatus.NEEDS_REVIEW
    assert "is on probation (2 wrong accepts)" in _queue(db, d)[0]
    # Policy rules are never held, whatever their row says.
    db.add(RuleHealth(rule="AGREEMENT_DEFAULT_1_YEAR", state="probation"))
    db.query(RuleHealth).filter_by(rule="AGREEMENT_THIS_DAY_OF").delete()
    db.flush()
    ok = _store(db, _csp(db), issue, issue + timedelta(days=365), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR")
    assert ok.status == DocumentStatus.VALID


def test_the_upload_page_says_our_team_is_checking_not_accepted(client, monkeypatch):
    c, Session = client
    cid, _, token = _portal_csp(Session)
    real = safety_gate.assess

    def risky(*a, **k):
        r = real(*a, **k)
        return {**r, "score": 100, "level": "hold", "reasons": [{"code": "test", "points": 100, "text": "test"}]}
    monkeypatch.setattr(safety_gate, "assess", risky)
    issue = TODAY - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["message"]["en"].startswith("Document received. Our team will check")
    assert "issue_date" not in res and "expiry_date" not in res
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status == DocumentStatus.NEEDS_REVIEW
    [q] = s.query(ManualReviewQueue).filter_by(document_id=d.id).all()
    assert q.reason.startswith("Safety check: risk 100: test.")
    s.close()


def test_a_term_starting_after_arrival_counts_and_a_person_checks_it(db_session):
    db = db_session
    soon = _store(db, _csp(db), TODAY + timedelta(days=2), TODAY + timedelta(days=2 + 3 * 365),
                  date_source="AGREEMENT_COMMENCING_DATE", validity_rule_used="AGREEMENT_RANGE_FROM_TO")
    risk = soon.extracted_fields["risk"]
    assert risk["level"] == "check" and [r["code"] for r in risk["reasons"]] == ["starts_later"]
    assert soon.status == DocumentStatus.VALID and "renewal signed in advance" in _queue(db, soon)[0]
    # Months ahead is still impossible: held.
    far = _store(db, _csp(db), TODAY + timedelta(days=90), TODAY + timedelta(days=90 + 3 * 365),
                 date_source="AGREEMENT_COMMENCING_DATE", validity_rule_used="AGREEMENT_RANGE_FROM_TO")
    assert far.status == DocumentStatus.NEEDS_REVIEW and "after it arrived" in _queue(db, far)[0]
