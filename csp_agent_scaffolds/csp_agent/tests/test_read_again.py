"""A file stored as unreadable is read again when the same file comes again,
instead of being turned away on the old verdict (Rahul Kumar 1A852163: an
agreement emailed on 14-09-2026, unreadable with the rules of the time, sent
again three times by the DC on 30-10 and refused each time as "the same file
could not be read before", although today's rules read it: 14-09-2026, valid
for 3 years)."""
import uuid
from datetime import date, timedelta

from app.document_service import store_extracted_document
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue
from tests.test_gmail_ingest import fake_gmail  # noqa: F401  (fixture)
from tests.test_portal_and_api import _csp as _portal_csp, _form, _pvr_pdf, client  # noqa: F401  (fixture)

TODAY = date.today()


def _csp(db):
    code = f"6R{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Rahul Test", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


UNREADABLE = {"readability": "UNREADABLE", "document_type": "AGREEMENT", "extraction_method": "UNREADABLE_NO_ISSUE_DATE",
              "readability_reason": "Issue date could not be read (blank or unclear date field).", "page_count": 15,
              "pages_readable": 12}


def _readable(issue):
    return {"readability": "READABLE", "document_type": "AGREEMENT", "start_date": issue.isoformat(),
            "expiry_date": issue.replace(year=issue.year + 3).isoformat(), "date_source": "AGREEMENT_THIS_DAY_OF",
            "validity_rule_used": "AGREEMENT_EXPLICIT_3_YEAR", "has_explicit_3year_clause": True, "confidence": 0.95,
            "page_count": 15, "pages_readable": 2,
            "match_text": f"On this day of, {issue:%d/%m/%Y} (Effective Date), CSP Name RAHUL KUMAR. "
                          "Valid for a period of Three (3) Years from the Effective Date"}


def test_the_same_file_read_again_replaces_the_unreadable_verdict(db_session):
    db = db_session
    c = _csp(db)
    data = f"%PDF-1.4 agreement {uuid.uuid4().hex}".encode()
    first = store_extracted_document(db, c, data, "New Doc.pdf", "application/pdf", UNREADABLE, channel="GMAIL_INBOUND")
    old = first.document
    assert old.readability == "UNREADABLE" and old.status == DocumentStatus.UNREADABLE
    # Sent again, still unreadable: the same document, nothing changes.
    again = store_extracted_document(db, c, data, "agree.pdf", "application/pdf", UNREADABLE, channel="DC_MASTER_LINK")
    assert again.decision == "DUPLICATE" and again.document.id == old.id
    # Sent again, and today's rules read it: the same row, now readable and counted.
    issue = TODAY - timedelta(days=46)
    out = store_extracted_document(db, c, data, "agree.pdf", "application/pdf", _readable(issue), channel="DC_MASTER_LINK")
    db.flush()
    d = out.document
    assert out.decision == "READABLE" and d.id == old.id
    assert d.readability == "READABLE" and d.status == DocumentStatus.VALID and d.is_current
    assert d.issue_date == issue and d.validity_rule_used == "AGREEMENT_EXPLICIT_3_YEAR"
    assert d.upload_channel == "GMAIL_INBOUND"                      # where it first came from is kept
    assert "read again" in d.extracted_fields["read_again"].lower() and d.extracted_fields["risk"]["level"] == "low"
    assert db.query(Document).filter_by(csp_id=c.id).count() == 1
    # A readable copy is never replaced by sending it again.
    later = store_extracted_document(db, c, data, "agree.pdf", "application/pdf", _readable(issue), channel="GMAIL_INBOUND")
    assert later.decision == "DUPLICATE"


def test_the_upload_page_reads_a_once_unreadable_file_again(client):
    c, Session = client
    cid, _, token = _portal_csp(Session)
    issue = TODAY - timedelta(days=20)
    pdf = _pvr_pdf(issue)
    s = Session()
    old = store_extracted_document(s, s.get(CSP, cid), pdf, "pvr.pdf", "application/pdf",
                                   {**UNREADABLE, "document_type": "POLICE_VERIFICATION"}, channel="GMAIL_INBOUND").document
    s.commit()
    old_id = old.id
    s.close()
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": ("pvr.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["ok"] and res["issue_date"] == issue.isoformat()                 # not "could not be read before"
    s = Session()
    d = s.get(Document, old_id)
    assert d.readability == "READABLE" and d.status == DocumentStatus.VALID and d.is_current
    assert s.query(Document).filter_by(csp_id=cid).count() == 1
    assert not s.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == old_id,
                                                 ManualReviewQueue.reason.like("Safety check%")).first()
    s.close()


def test_an_email_with_a_once_unreadable_file_is_read_again(db_session, fake_gmail):
    from app import gmail_ingest
    from tests.test_gmail_ingest import _csp as _mail_csp, _mail
    mailbox, _ = fake_gmail
    db = db_session
    c = _mail_csp(db)
    issue = TODAY - timedelta(days=5)
    pdf = _pvr_pdf(issue, c.name)
    old = store_extracted_document(db, c, pdf, "pvr.pdf", "application/pdf",
                                   {**UNREADABLE, "document_type": "POLICE_VERIFICATION"}, channel="GMAIL_INBOUND").document
    db.flush()
    mid = "r" + uuid.uuid4().hex[:8]
    _mail(mailbox, mid, c.current_code, c.name, {"pvr.pdf": pdf})
    assert gmail_ingest.process_message(db, None, mid) == "PROCESSED"
    d = db.get(Document, old.id)
    assert d.readability == "READABLE" and d.issue_date == issue
    assert db.query(Document).filter_by(csp_id=c.id).count() == 1
