"""Documents filed under the wrong CSP (app/owner_guard.py): KO codes in file
names, the check on the CSP's own link, the Review queue warning and Move,
and scripts/fix_misfiled_documents.py."""
import sys
import uuid
from datetime import date, timedelta

from app import auth
from app.gmail_ingest import check_owner
from app.models import CSP, CSPCodeHistory, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from tests.test_portal_and_api import _csp, _form, _pvr_pdf, client  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}
TODAY = date.today()


def _two(s, name_a="Tanishka Yadav", name_b="Ashwani Kumar"):
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 1000000
    a = CSP(name=f"{name_a}", current_code=f"7G{n:06d}", lookup_code=f"7G{n:06d}", is_active_in_calling_sheet=True)
    b = CSP(name=f"{name_b}", current_code=f"7H{n:06d}", lookup_code=f"7H{n:06d}", is_active_in_calling_sheet=True)
    s.add_all([a, b])
    s.flush()
    return a, b


def _doc(s, csp, filename, status=DocumentStatus.VALID, current=True, sha=None, t="AGREEMENT"):
    d = Document(csp_id=csp.id, document_type=t, sha256=sha or uuid.uuid4().hex, readability="READABLE",
                 status=status, is_current=current, issue_date=TODAY - timedelta(days=30),
                 expiry_date=TODAY + timedelta(days=700), original_filename=filename, upload_channel="GMAIL_INBOUND")
    s.add(d)
    s.flush()
    return d


def test_the_file_name_says_whose_document_it_is(db_session):
    s = db_session
    a, b = _two(s)
    ex = {"readability": "READABLE", "match_text": "CUSTOMER SERVICE POINT AGREEMENT EKO INDIA"}
    owner, how, review = check_owner(s, a, ex, [a, b], filename=f"{b.current_code} ASHWANI KUMAR-rotated.pdf")
    assert (owner.id, how, review) == (b.id, "CODE_IN_FILENAME", None)
    # This CSP's name is on it but the file name shows another CSP: a person decides.
    named = {**ex, "match_text": "CSP NAME TANISHKA YADAV"}
    owner, how, review = check_owner(s, a, named, [a, b], filename=f"{b.current_code}.pdf")
    assert owner.id == a.id and review.startswith("Document shows CSP codes") and b.current_code in review
    # Its own code in the file name, or none: as before.
    assert check_owner(s, a, named, [a, b], filename=f"{a.current_code}.pdf")[2] is None
    assert check_owner(s, a, named, [a, b], filename="scan.pdf")[2] is None


def test_own_link_turns_away_another_csps_document(client):
    c, Session = client
    cid, code, token = _csp(Session)
    _, other, _ = _csp(Session)
    issue = TODAY - timedelta(days=20)
    r = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
               files={"pvr_file": (f"PVR {other}.pdf", _pvr_pdf(issue), "application/pdf")}).json()["results"][0]
    assert not r["ok"] and "another CSP" in r["message"]["en"] and other in r["message"]["en"], r
    s = Session()
    assert s.query(Document).filter_by(csp_id=cid).count() == 0
    s.close()
    ok = c.post("/api/portal/upload", data=_form(token, pvr_issue_date=issue.isoformat()),
                files={"pvr_file": (f"PVR {code}.pdf", _pvr_pdf(issue), "application/pdf")}).json()["results"][0]
    assert ok["ok"]                                         # its own code: taken as before


def test_review_shows_whose_it_is_refuses_accept_and_moves_it(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    a, b = _two(s)
    d = _doc(s, a, f"{b.current_code} ASHWANI KUMAR-rotated.pdf", status=DocumentStatus.NEEDS_REVIEW, current=False)
    q = ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason="Re-read with the current rules: dates")
    s.add(q)
    s.commit()
    ids = (a.id, b.id, d.id, q.id)
    s.close()
    a_id, b_id, d_id, q_id = ids
    row = next(r for r in c.get("/api/hub/review", headers=HDR).json()["rows"] if r["id"] == q_id)
    assert row["owner_warning"]["move_to"]["id"] == b_id and "Ashwani Kumar" in row["owner_warning"]["text"]
    r = c.post(f"/api/hub/review/{q_id}/resolve", json={"accept": True}, headers=HDR)
    assert r.status_code == 409 and "Move it" in r.json()["detail"]
    r = c.post(f"/api/hub/review/{q_id}/move", headers=HDR)
    assert r.status_code == 200 and r.json()["result"] == "MOVED"
    s = Session()
    d = s.get(Document, d_id)
    assert d.csp_id == b_id and d.status == DocumentStatus.NEEDS_REVIEW and d.extracted_fields["moved_from"]
    assert s.get(ManualReviewQueue, q_id).status == ReviewStatus.REJECTED
    moved = s.query(ManualReviewQueue).filter_by(document_id=d_id, status=ReviewStatus.PENDING).one()
    assert moved.reason.startswith("Moved from") and "Tanishka Yadav" in moved.reason
    s.close()
    row = next(r for r in c.get("/api/hub/review", headers=HDR).json()["rows"] if r["id"] == moved.id)
    assert row["owner_warning"] is None                      # under its owner now: a normal check


def test_it_is_this_csps_accepts_and_the_warning_goes(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    a, b = _two(s)
    d = _doc(s, a, f"old {b.current_code}.pdf", status=DocumentStatus.NEEDS_REVIEW, current=False)
    q = ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason="check")
    s.add(q)
    s.commit()
    q_id, d_id = q.id, d.id
    s.close()
    assert c.post(f"/api/hub/review/{q_id}/resolve", json={"accept": True, "owner_ok": True},
                  headers=HDR).status_code == 200
    s = Session()
    d = s.get(Document, d_id)
    assert d.status == DocumentStatus.MANUAL_VERIFIED and d.extracted_fields["owner_confirmed"]
    s.close()


def _script(monkeypatch, test_engine, tmp_path, *args):
    from sqlalchemy.orm import sessionmaker
    from scripts import fix_misfiled_documents as f
    monkeypatch.chdir(tmp_path)                              # its Excel list goes to logs/ here
    monkeypatch.setattr(f, "SessionLocal", sessionmaker(bind=test_engine))
    monkeypatch.setattr(sys, "argv", ["fix_misfiled_documents", *args])
    f.main()


def test_script_moves_rejects_and_asks(db_session, test_engine, monkeypatch, tmp_path, capsys):
    s = db_session
    a, b = _two(s)                                           # a holds b's agreement (counted for a)
    moved = _doc(s, a, f"{b.current_code} ASHWANI KUMAR-rotated.pdf")
    c_, _ = _two(s, "Rajeev Srivastav", "Unused One")
    shared = uuid.uuid4().hex
    _doc(s, b, "inbound1.pdf", sha=shared, current=False)    # b already has this file
    copy = _doc(s, c_, f"{b.current_code} ASHWANI KUMAR (1).pdf", sha=shared, current=False)
    d1, d2 = _two(s, "Ram Lal", "Shyam Lal")
    both = uuid.uuid4().hex
    ask1 = _doc(s, d1, "scan.pdf", sha=both)
    _doc(s, d2, "scan.pdf", sha=both)
    e, f_ = _two(s, "Old Code Person", "New Holder")         # e's old code is f's code now: not "another CSP"
    s.add(CSPCodeHistory(csp_id=e.id, code=f_.current_code, reason="recoded"))
    kept = _doc(s, e, f"{f_.current_code}.pdf")
    s.commit()
    for code in (a.current_code, c_.current_code, d1.current_code, e.current_code):
        _script(monkeypatch, test_engine, tmp_path, "--csp", code)
    out = capsys.readouterr().out
    assert "Dry run" in out and "MOVE" in out and "REJECT" in out and "ASK" in out
    assert "\n0 document(s): 0 to move" in out                # e: nothing (its own old code)
    assert list((tmp_path / "logs").glob("misfiled_documents_*.xlsx"))
    s.expire_all()
    assert s.get(Document, moved.id).csp_id == a.id           # dry run: unchanged
    for code in (a.current_code, c_.current_code, d1.current_code):
        _script(monkeypatch, test_engine, tmp_path, "--csp", code, "--apply")
    s.expire_all()
    m = s.get(Document, moved.id)
    assert m.csp_id == b.id and m.status == DocumentStatus.NEEDS_REVIEW
    assert s.get(CSP, a.id).category == 4                    # a's only document was b's: live Label drops
    assert s.get(Document, copy.id).status == DocumentStatus.REJECTED and s.get(Document, copy.id).csp_id == c_.id
    ask = s.query(ManualReviewQueue).filter_by(document_id=ask1.id, status=ReviewStatus.PENDING).one()
    assert ask.reason.startswith("Whose document is it?") and d2.current_code in ask.reason
    assert s.get(Document, ask1.id).status == DocumentStatus.VALID     # asking changes nothing else
    assert s.get(Document, kept.id).csp_id == e.id
