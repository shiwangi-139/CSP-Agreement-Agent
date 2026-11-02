"""Upload page documents read after the quick checks (app/upload_jobs.py): a
slow read answers "received, being checked" and the page asks again; a read
that keeps failing goes to a person; the worker picks up what the web app left."""
import time
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app import upload_jobs
from app.api import portal
from app.models import AgreementEvent, Document, DocumentStatus, ManualReviewQueue, UploadJob, UploadJobFile
from tests.test_portal_and_api import _csp, _form, _pvr_pdf, client  # noqa: F401  (fixture)


def _status(c, key, seconds=20):
    until = time.time() + seconds
    while time.time() < until:
        j = c.get(f"/api/portal/upload-status?job={key}").json()
        if j["done"]:
            return j
        time.sleep(0.2)
    raise AssertionError("upload job not done")


def test_a_slow_read_answers_received_and_the_page_gets_the_result_later(client, monkeypatch):
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    real = portal.extract_document_fields_deterministic

    def slow(*a, **k):
        time.sleep(1.5)
        return real(*a, **k)
    monkeypatch.setattr(portal, "extract_document_fields_deterministic", slow)
    monkeypatch.setattr(portal, "UPLOAD_WAIT_SECONDS", 0.2)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["pending"] and j["results"] == [{"section": "pvr", "ok": True, "pending": True,
                                                "message": portal._msg("reading")}]
    done = _status(c, j["pending"])
    [res] = done["results"]
    assert res["ok"] and res["message"] == portal._msg("accepted") and res["issue_date"] == issue.isoformat()
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status == DocumentStatus.VALID and d.is_current
    job = s.query(UploadJob).filter_by(key=j["pending"]).one()
    assert job.status == "DONE" and job.attempts == 1
    assert s.query(UploadJobFile).filter_by(job_id=job.id).count() == 0          # the waiting copy is gone
    assert s.query(AgreementEvent).filter_by(csp_id=cid, event_type="PORTAL_UPLOAD").count() == 1
    s.close()
    assert c.get("/api/portal/upload-status?job=nosuchjobkey123").status_code == 404


def test_a_quick_read_answers_at_once_as_before(client):
    c, Session = client
    _, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat(), iibf_issue_date="2099-01-01"),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf"),
                      "iibf_file": ("i.pdf", _pvr_pdf(issue), "application/pdf")})
    j = r.json()
    assert "pending" not in j
    # Order kept: the quick check's answer (a date in the future) and the read one.
    assert [(x["section"], x["ok"]) for x in j["results"]] == [("pvr", True), ("iibf", False)]


def test_a_read_that_keeps_failing_goes_to_a_person_not_back_to_the_csp(client, monkeypatch):
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    calls = []

    def broken(*a, **k):
        calls.append(1)
        raise RuntimeError("OCR engine crashed")
    monkeypatch.setattr(portal, "extract_document_fields_deterministic", broken)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", _pvr_pdf(issue), "application/pdf")})
    j = r.json()
    if j.get("pending"):
        j = _status(c, j["pending"])
    [res] = j["results"]
    assert len(calls) == upload_jobs.MAX_ATTEMPTS
    assert res["ok"] and res["message"] == portal._msg("received_check")
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.status == DocumentStatus.NEEDS_REVIEW
    q = s.query(ManualReviewQueue).filter_by(document_id=d.id).one()
    assert "could not read this upload" in q.reason and "OCR engine crashed" in q.reason
    ev = s.query(AgreementEvent).filter_by(csp_id=cid, event_type="UPLOAD_FAILED").one()
    assert ev.payload["kind"] == "reading_failed" and ev.payload["side"] == "OUR"
    assert s.query(UploadJob).filter_by(csp_id=cid).one().status == "FAILED"
    s.close()


def test_the_worker_reads_a_job_the_web_app_left(client, monkeypatch):
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=20)
    s = Session()
    job = upload_jobs.enqueue(s, cid, token, "CSP_UPLOAD_PORTAL", [], [dict(
        section="pvr", doc_type="POLICE_VERIFICATION", typed=issue, filename="pvr.pdf", mime="application/pdf",
        n_files=1, data=_pvr_pdf(issue))], {"name": "Portal CSP"})
    s.commit()
    # Queued 5 minutes ago, and the web app restarted before reading it.
    s.execute(text("UPDATE upload_jobs SET created_at = now() - interval '5 minutes' WHERE id = :i"), {"i": job.id})
    s.commit()
    key = job.key
    s.close()
    assert upload_jobs.sweep(Session)["read"] >= 1
    j = c.get(f"/api/portal/upload-status?job={key}").json()
    assert j["done"] and j["results"][0]["ok"]
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).one().status == DocumentStatus.VALID
    s.close()


def test_a_job_is_read_once_even_when_two_readers_try(client):
    c, Session = client
    cid, _, token = _csp(Session)
    s = Session()
    job = upload_jobs.enqueue(s, cid, token, "CSP_UPLOAD_PORTAL", [], [], None)
    s.commit()
    assert upload_jobs.claim(s, job.id) is True
    assert upload_jobs.claim(s, job.id) is False
    s.close()
