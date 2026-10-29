"""app/safety_gate.py: a reading that cannot be right is not accepted; it
goes to the Review queue and the CSP is told our team is checking it."""
import uuid
from datetime import date, timedelta

from app import safety_gate
from app.compliance import evaluate
from app.document_service import store_extracted_document
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue
from app.signals import review_cause
from tests.test_portal_and_api import _csp as _portal_csp, _form, _pvr_pdf, client  # noqa: F401  (fixture)

TODAY = date.today()


def _csp(db):
    code = f"5G{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Gate CSP", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def _store(db, csp, issue, expiry, doc_type="AGREEMENT"):
    ex = {"readability": "READABLE", "document_type": doc_type, "start_date": issue.isoformat(),
          "expiry_date": expiry.isoformat(), "date_source": "AGREEMENT_EXECUTION_DATE"}
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


def test_a_good_reading_is_accepted_as_before(db_session):
    db = db_session
    d = _store(db, _csp(db), TODAY - timedelta(days=30), TODAY + timedelta(days=3 * 365))
    assert d.status == DocumentStatus.VALID and "safety_check" not in d.extracted_fields
    assert not any((r or "").startswith("Safety check") for r in _queue(db, d))


def test_impossible_readings_are_held_for_a_person(db_session):
    db = db_session
    c = _csp(db)
    held = _store(db, c, TODAY - timedelta(days=30), TODAY - timedelta(days=60))
    assert held.status == DocumentStatus.NEEDS_REVIEW and not held.is_current
    [reason] = _queue(db, held)
    assert reason.startswith("Safety check: expiry") and "not told it is accepted" in reason
    assert held.extracted_fields["safety_check"] == reason
    assert review_cause(reason) == "held: impossible dates"
    # It doesn't count until a person accepts it, and no reminder asks for it meanwhile.
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


def test_the_upload_page_says_our_team_is_checking_not_accepted(client, monkeypatch):
    c, Session = client
    cid, _, token = _portal_csp(Session)
    monkeypatch.setattr(safety_gate, "check", lambda *a, **k: "Safety check: test.")
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
    assert [q.reason for q in s.query(ManualReviewQueue).filter_by(document_id=d.id)] == ["Safety check: test."]
    s.close()
