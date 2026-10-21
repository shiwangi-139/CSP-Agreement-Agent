"""The DC master link: one link for every working CSP; locked documents,
duplicates and the Review queue listing."""
from datetime import date, timedelta

from app import auth
from app.api import portal
from app.master_link import RENEW_WITHIN_DAYS
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from tests.test_portal_and_api import _csp, _pvr_pdf, client  # noqa: F401  (fixture)


def _link(Session):
    from app import master_link
    s = Session()
    url = master_link.create(s, "test")
    s.commit()
    s.close()
    return url.split("dc=")[1]


def _valid_doc(Session, cid, doc_type, expiry):
    s = Session()
    s.add(Document(csp_id=cid, document_type=doc_type, status=DocumentStatus.VALID, is_current=True,
                   readability="READABLE", issue_date=expiry - timedelta(days=365), expiry_date=expiry,
                   sha256=f"sha-{cid}-{doc_type}"))
    s.commit()
    s.close()


def _upload(c, dc, code, issue, data, **kw):
    return c.post("/api/portal/upload", data={"dc": dc, "code": code, "pvr_issue_date": issue.isoformat(), **kw},
                  files={"pvr_file": ("pvr.pdf", data, "application/pdf")})


def test_page_and_context_need_a_live_link(client):
    c, Session = client
    _, code, _ = _csp(Session)
    assert "Get your upload link" in c.get("/upload?dc=nope").text
    dc = _link(Session)
    page = c.get(f"/upload?dc={dc}").text
    assert code in page and "__CONTEXT_JSON__" not in page
    r = c.post("/upload", data={"action": "dc_context", "dc": dc, "code": code})
    assert r.status_code == 200 and r.json()["context"]["csp"]["code"] == code
    assert c.post("/upload", data={"action": "dc_context", "dc": dc, "code": "1A000000"}).status_code == 404
    assert c.post("/upload", data={"action": "dc_context", "dc": "nope", "code": code}).status_code == 403
    newer = _link(Session)                                            # a new link switches the old one off
    assert c.post("/upload", data={"action": "dc_context", "dc": dc, "code": code}).status_code == 403
    assert c.post("/upload", data={"action": "dc_context", "dc": newer, "code": code}).status_code == 200


def test_document_valid_for_long_is_locked_but_one_expiring_soon_is_open(client):
    c, Session = client
    cid, code, _ = _csp(Session)
    _valid_doc(Session, cid, "POLICE_VERIFICATION", date.today() + timedelta(days=200))
    _valid_doc(Session, cid, "IIBF_CERTIFICATE", date.today() + timedelta(days=RENEW_WITHIN_DAYS - 5))
    dc = _link(Session)
    docs = {d["type"]: d for d in c.post("/upload", data={"action": "dc_context", "dc": dc, "code": code})
            .json()["context"]["docs"]}
    assert docs["POLICE_VERIFICATION"]["locked"] and "valid till" in docs["POLICE_VERIFICATION"]["lock_message"]["en"]
    assert not docs["IIBF_CERTIFICATE"]["locked"] and not docs["AGREEMENT"]["locked"]
    issue = date.today() - timedelta(days=10)
    res = _upload(c, dc, code, issue, _pvr_pdf(issue)).json()["results"][0]
    assert not res["ok"] and "valid till" in res["message"]["en"]     # the server enforces the lock too


def test_dc_upload_is_stored_counted_and_listed_for_review(client):
    c, Session = client
    cid, code, _ = _csp(Session)
    dc = _link(Session)
    issue = date.today() - timedelta(days=20)
    r = _upload(c, dc, code, issue, _pvr_pdf(issue))
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    s = Session()
    d = s.query(Document).filter_by(csp_id=cid).one()
    assert d.upload_channel == "DC_MASTER_LINK" and d.status == DocumentStatus.VALID
    q = s.query(ManualReviewQueue).filter_by(document_id=d.id, status=ReviewStatus.PENDING).one()
    assert q.reason.startswith("DC upload") and "already counted" in q.reason
    s.close()


def test_file_on_record_for_another_csp_is_refused(client):
    c, Session = client
    _, code_a, _ = _csp(Session)
    cid_b, code_b, _ = _csp(Session)
    dc = _link(Session)
    issue = date.today() - timedelta(days=20)
    data = _pvr_pdf(issue)
    assert _upload(c, dc, code_a, issue, data).json()["results"][0]["ok"]
    res = _upload(c, dc, code_b, issue, data).json()["results"][0]
    assert not res["ok"] and res["message"]["en"] == portal.MSG["dup_other"][1]
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid_b).count() == 0
    s.close()


def test_excel_lists_only_the_working_csps(client):
    c, Session = client
    _, code, _ = _csp(Session)
    dc = _link(Session)
    r = c.post("/upload", data={"action": "dc_report", "dc": dc})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.post("/upload", data={"action": "dc_report", "dc": "nope"}).status_code == 403


def test_admin_makes_and_switches_off_the_link(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    hdr = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
    c, _ = client
    made = c.post("/api/hub/master-link", headers=hdr)
    assert made.status_code == 200 and "/upload?dc=" in made.json()["url"] and made.json()["active"]
    assert c.get("/api/hub/master-link", headers=hdr).json()["active"]
    assert c.post("/api/hub/master-link/revoke", headers=hdr).json()["revoked"] >= 1
    assert not c.get("/api/hub/master-link", headers=hdr).json()["active"]
