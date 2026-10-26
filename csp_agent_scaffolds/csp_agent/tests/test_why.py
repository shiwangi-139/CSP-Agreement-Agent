"""The plain-words 'why' saved with each reading (app/why.py) and the
reading-evaluation score in the Agent Coach."""
from datetime import date, datetime

from app import insights, why
from app.models import MonitorRun
from tests.test_portal_and_api import client  # noqa: F401  (fixture)


def test_why_names_the_date_the_rule_the_text_and_what_was_ignored():
    text = ('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of 15.06.2025,("Effective Date") CSP Name SHATRUGHNA RAY\n'
            "This agreement would be Valid for Two Years from the date of signing.\nvivo Y200 5G\nOct 24, 2026, 17:13\n")
    w = why.explain({"document_type": "AGREEMENT", "start_date": "2025-06-15", "expiry_date": "2027-06-15",
                     "date_source": "AGREEMENT_HEADING_DATE", "validity_rule_used": "AGREEMENT_RANGE_FROM_TO",
                     "match_text": text, "page_count": 3}, date(2025, 6, 20))
    issue, valid = w["lines"]
    assert issue["value"] == "15-06-2025" and "heading" in issue["how"] and "15.06.2025" in issue["text"]
    assert valid["value"] == "15-06-2027" and "Two Years" in valid["text"]
    joined = " ".join(w["ignored"])
    assert "camera stamp" in joined and "20-06-2025" in joined and "3 page" in joined
    assert w["summary"].startswith("CSP Agreement, issued 15-06-2025, valid till 15-06-2027")


def test_why_for_a_default_validity_and_no_date():
    w = why.explain({"document_type": "POLICE_VERIFICATION", "start_date": "2026-04-24", "expiry_date": "2027-04-24",
                     "date_source": "PVR_DIGITAL_SIGNATURE_DATE", "validity_rule_used": "PVR_DEFAULT_1_YEAR",
                     "match_text": "Digitally Signed by X Date:24/4/2026 5:57:19 PM"})
    assert "digital signature" in w["lines"][0]["how"] and "Eko's rule" in w["lines"][1]["how"] and w["lines"][1]["text"] is None
    assert "no issue date" in why.explain({"document_type": "IIBF_CERTIFICATE"})["summary"]


def test_saved_with_the_document_and_shown(client, monkeypatch):
    from app import auth
    from tests.test_portal_and_api import _csp, _form, _pvr_pdf
    from datetime import timedelta
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    assert c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
                  files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")}).json()["results"][0]["ok"]
    docs = c.get(f"/api/hub/csp/{cid}", headers={"X-API-Key": "k"}).json()["documents"]
    w = docs[0]["why"]
    assert w and w["lines"][0]["value"] == issue.strftime("%d-%m-%Y") and w["lines"][0]["text"]


def test_coach_reports_the_nightly_reading_score(db_session):
    db = db_session
    db.add(MonitorRun(kind="reading_eval", started_at=datetime.utcnow(), status="OK", summary={
        "set": "holdout", "previous_score": 14, "fixed": ["1", "2"], "broken": [],
        "by_type": {"ALL": {"documents": 80, "WRONG ACCEPT": 0, "wrong reject": 3, "score": 3}}}))
    db.flush()
    f = next(x for x in insights.coach(db) if x["title"].startswith("Reading score"))
    assert f["level"] == "warn" and "(was 14)" in f["title"] and "hold-out" in f["title"] and "fixed since the last run 2" in f["detail"]
