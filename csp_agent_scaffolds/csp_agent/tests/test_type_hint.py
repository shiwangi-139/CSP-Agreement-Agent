"""The file name as a hint for the type (app/type_hint.py), the one-time fix
(scripts/fix_type_from_filename.py), and Pending by RM without Inactive
terminals."""
import sys
import uuid
from datetime import date, timedelta

from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from app.type_hint import recheck_with_name, type_named_in
from tests.test_portal_and_api import _pvr_pdf
from tests.test_same_file_other_type import _agreement

ISSUE = date.today() - timedelta(days=200)


def test_the_type_a_file_name_says():
    assert type_named_in("1A850853 Agreement MATHURA PRASAD.pdf") == "AGREEMENT"
    assert type_named_in("CSP Agreement Sunita KO 1A852309.pdf") == "AGREEMENT"
    assert type_named_in("PVR1A852437.pdf") == type_named_in("PCC SADA.pdf") == "POLICE_VERIFICATION"
    assert type_named_in("police vericafaction.pdf") == "POLICE_VERIFICATION"
    assert type_named_in("IIBF.pdf") == "IIBF_CERTIFICATE"
    assert type_named_in("agreement and PVR.pdf") is None and type_named_in("scan.pdf") is None


def test_a_file_named_agreement_read_as_a_pvr_is_read_again():
    ex = {"readability": "READABLE", "document_type": "POLICE_VERIFICATION", "start_date": "2024-07-24"}
    agreement = lambda data, name, **kw: {"readability": "READABLE", "document_type": "AGREEMENT", "start_date": "2024-07-24"}
    out = recheck_with_name(b"x", "1A850853 Agreement MATHURA PRASAD.pdf", ex, read=agreement)
    assert out["document_type"] == "AGREEMENT" and "file name says" in out["type_hint"]
    still_pvr = lambda data, name, **kw: dict(ex)
    out = recheck_with_name(b"x", "Agreement.pdf", ex, read=still_pvr)
    assert out["document_type"] == "POLICE_VERIFICATION" and "check the type" in out["type_review"]
    assert recheck_with_name(b"x", "PVR.pdf", ex, read=None) is ex          # agrees: nothing read again


def _saved(s, csp, data, tmp_path, filename, t="POLICE_VERIFICATION"):
    import hashlib
    f = tmp_path / f"{uuid.uuid4().hex}.pdf"
    f.write_bytes(data)
    d = Document(csp_id=csp.id, document_type=t, status=DocumentStatus.VALID, is_current=True, readability="READABLE",
                 issue_date=ISSUE, expiry_date=ISSUE + timedelta(days=365), sha256=hashlib.sha256(data).hexdigest(),
                 original_filename=filename, storage_path=str(f), mime_type="application/pdf",
                 upload_channel="GMAIL_INBOUND")
    s.add(d)
    s.flush()
    return d


def test_script_corrects_or_asks(db_session, test_engine, monkeypatch, tmp_path, capsys):
    from sqlalchemy.orm import sessionmaker
    from scripts import fix_type_from_filename as f
    s = db_session
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 1000000
    a = CSP(name="Sunita Rani", current_code=f"7S{n:06d}", is_active_in_calling_sheet=True)
    b = CSP(name="Akhalesh Kumar", current_code=f"7T{n:06d}", is_active_in_calling_sheet=True)
    s.add_all([a, b])
    s.flush()
    agr = _saved(s, a, _agreement(a.current_code, a.name), tmp_path, f"CSP Agreement Sunita KO {a.current_code}.pdf")
    pvr = _saved(s, b, _pvr_pdf(ISSUE, holder="AKHALESH KUMAR"), tmp_path, f"{b.current_code} CSP AGREEMENT.pdf")
    s.commit()
    monkeypatch.setattr(f, "SessionLocal", sessionmaker(bind=test_engine))
    for code in (a.current_code, b.current_code):
        monkeypatch.setattr(sys, "argv", ["fix_type_from_filename", "--csp", code])
        f.main()
    out = capsys.readouterr().out
    assert "RETYPE" in out and "REVIEW" in out and "Dry run" in out
    for code in (a.current_code, b.current_code):
        monkeypatch.setattr(sys, "argv", ["fix_type_from_filename", "--csp", code, "--apply"])
        f.main()
    s.expire_all()
    d = s.get(Document, agr.id)
    assert d.document_type == "AGREEMENT" and d.is_current and "type check by file name" in d.extracted_fields["read_again"]
    d = s.get(Document, pvr.id)
    assert d.document_type == "POLICE_VERIFICATION" and d.status == DocumentStatus.NEEDS_REVIEW and not d.is_current
    q = s.query(ManualReviewQueue).filter_by(document_id=pvr.id, status=ReviewStatus.PENDING).one()
    assert "file name" in q.reason and "Check the type" in q.reason


def test_pending_by_rm_leaves_out_inactive_terminals(db_session):
    from app.rm_pending import _csps
    s = db_session
    tag = uuid.uuid4().hex[:6]
    n = int(tag, 16) % 1000000
    working = CSP(name="Working One", current_code=f"7W{n:06d}", category=2, is_active_in_calling_sheet=True)
    off = CSP(name="Inactive One", current_code=f"7X{n:06d}", category=2, is_active_in_calling_sheet=True,
              terminal_status="Inactive")
    made_active = CSP(name="Made Active", current_code=f"7Y{n:06d}", category=2, is_active_in_calling_sheet=True,
                      terminal_status="Inactive", active_override=True)
    s.add_all([working, off, made_active])
    s.flush()
    ids = {c.id for c in _csps(s)}
    assert working.id in ids and made_active.id in ids and off.id not in ids
