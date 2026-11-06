"""The same file on record as ANOTHER type (1A850853 Mathura Prasad: his
agreement, emailed as '1A850853 Agreement MATHURA PRASAD.pdf', was saved as a
police certificate): uploaded again in the Agreement box it is read again as
an agreement, not answered 'we already have this file'."""
import hashlib
from datetime import date, timedelta

from app.compliance import evaluate
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from tests.test_master_link import _link
from tests.test_portal_and_api import _csp, client  # noqa: F401  (fixture)

ISSUE = date.today() - timedelta(days=200)


def _agreement(code: str, name: str) -> bytes:
    """A 12-page agreement naming this CSP (its own code: the owner check is sure)."""
    import pymupdf
    d = pymupdf.open()
    for i in range(12):
        p = d.new_page()
        p.insert_text((40, 60), "CUSTOMER SERVICE POINT AGREEMENT" if i == 0 else f"Clause page {i + 1}", fontsize=10)
        if i == 0:
            p.insert_text((40, 80), f'On this day of, {ISSUE:%d/%m/%Y} ("Effective Date"), CSP Name {name} '
                                    f'CSP Code {code}', fontsize=9)
            p.insert_text((40, 100), "valid for a period of three (3) years", fontsize=9)
    return d.tobytes()


def _saved_as_pvr(Session, cid, data, status):
    s = Session()
    d = Document(csp_id=cid, document_type="POLICE_VERIFICATION", status=status, is_current=False,
                 readability="READABLE", issue_date=ISSUE, expiry_date=ISSUE + timedelta(days=365),
                 sha256=hashlib.sha256(data).hexdigest(), original_filename="1A850853 Agreement MATHURA PRASAD.pdf",
                 upload_channel="GMAIL_INBOUND")
    s.add(d)
    s.commit()
    did, name = d.id, s.get(CSP, cid).name
    s.close()
    return did


def _upload_agreement(c, dc, code, data):
    return c.post("/api/portal/upload", data={"dc": dc, "code": code, "agreement_issue_date": ISSUE.isoformat()},
                  files={"agreement_file": ("agreement.pdf", data, "application/pdf")}).json()["results"][0]


def _setup(Session, status):
    cid, code, _ = _csp(Session)
    s = Session()
    name = s.get(CSP, cid).name
    s.close()
    data = _agreement(code, name)
    return cid, code, data, _saved_as_pvr(Session, cid, data, status)


def test_agreement_saved_as_a_pvr_is_read_again_as_an_agreement(client):
    c, Session = client
    cid, code, data, did = _setup(Session, DocumentStatus.EXPIRED)
    res = _upload_agreement(c, _link(Session), code, data)
    assert res["ok"] and "already have this file" not in res["message"]["en"], res
    s = Session()
    d = s.get(Document, did)
    assert d.document_type == "AGREEMENT" and "Saved as police verification" in d.extracted_fields["read_again"]
    assert evaluate(s, s.get(CSP, cid)).docs["AGREEMENT"].document.id == did      # counts as his agreement
    s.close()


def test_a_persons_decision_is_not_overturned_but_checked(client):
    c, Session = client
    cid, code, data, did = _setup(Session, DocumentStatus.MANUAL_VERIFIED)
    res = _upload_agreement(c, _link(Session), code, data)
    assert res["ok"] and "already have this file" not in res["message"]["en"], res
    s = Session()
    assert s.get(Document, did).document_type == "POLICE_VERIFICATION"
    q = s.query(ManualReviewQueue).filter_by(document_id=did, status=ReviewStatus.PENDING).one()
    assert "Uploaded again in the" in q.reason and "Check the type" in q.reason
    s.close()
