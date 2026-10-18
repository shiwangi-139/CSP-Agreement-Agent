"""scripts/reread_default_agreements.py: an agreement stored with the 1-year
default is re-read with the current rules (dry run first, then --apply)."""
import csv
import sys
import uuid
from datetime import date, datetime

import pymupdf
from sqlalchemy.orm import sessionmaker

from app.models import CSP, Document, DocumentStatus

AGREEMENT_TEXT = [
    "CUSTOMER SERVICE POINT AGREEMENT",
    "BY E-KO INDIA PRIVATE LIMITED SBI CUSTOMER SERVICE POINT AGREEMENT",
    'On this day of, 01-07-2025 ("Effective Date"), MOHAMMAD MOHSIN MOHAMMAD HASAN,',
    "S.B.I. C.S.P. KIOSK ID 1A851804, VALIDATION FOR YEAR's:- START DATE ON 01-07-2025",
    "TO END AND CLOSE DATE 01-06-2028 Full 3 year agreement.",
    "In consideration of the mutual promises made herein, the Parties agree as follows.",
    "E-KO India Financial Services Pvt. Ltd. Customer Service Point CSP shall provide services.",
]


def _pdf(path):
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(AGREEMENT_TEXT):
        page.insert_text((40, 60 + 20 * i), line, fontsize=9)
    d.save(path)


def _run(monkeypatch, db_session, *args):
    import scripts.reread_default_agreements as s
    monkeypatch.setattr(s, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(sys, "argv", ["reread", *args])
    s.main()


def test_reread_fixes_an_agreement_stored_with_the_one_year_default(db_session, tmp_path, monkeypatch, capsys):
    db = db_session
    code = f"9R{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Reread CSP", current_code=code, lookup_code=code, phone="9876543210",
              whatsapp_number="9876543210", is_active_in_calling_sheet=True)
    db.add(csp)
    db.flush()
    f = tmp_path / "agreement.pdf"
    _pdf(f)
    doc = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, status=DocumentStatus.EXPIRED,
                   issue_date=date(2025, 7, 1), expiry_date=date(2026, 7, 1), is_current=True,
                   readability="READABLE", validity_rule_used="AGREEMENT_DEFAULT_1_YEAR", storage_path=str(f))
    db.add(doc)
    db.commit()

    _run(monkeypatch, db, "--csp", code)                              # dry run
    assert "2025-07-01 -> 2028-06-01" in capsys.readouterr().out
    db.refresh(doc)
    assert doc.expiry_date == date(2026, 7, 1)                        # nothing changed

    _run(monkeypatch, db, "--csp", code, "--apply", "--no-message")
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.expiry_date == date(2028, 6, 1) and doc.status == DocumentStatus.VALID
    assert doc.validity_rule_used != "AGREEMENT_DEFAULT_1_YEAR"


def test_recheck_files_an_agreement_turned_away_as_expired_by_older_rules(db_session, monkeypatch, capsys):
    """The upload page turned the agreement away as "expired" (1-year default);
    with the printed end date it is valid till 2028 and gets filed."""
    from app import vault
    from app.models import AgreementEvent
    import scripts.recheck_rejected_uploads as rc
    db = db_session
    code = f"9E{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Expired-by-mistake CSP", current_code=code, lookup_code=code, phone="9876543210",
              whatsapp_number="9876543210", is_active_in_calling_sheet=True)
    db.add(csp)
    db.flush()
    kept = vault.ROOT / "_rejected" / code / "agreement.pdf"
    kept.parent.mkdir(parents=True)
    _pdf(kept)
    db.add(AgreementEvent(csp_id=csp.id, event_type="PORTAL_UPLOAD", source="CSP_UPLOAD_PORTAL", channel="WEB",
                          payload={"results": [{"section": "agreement", "ok": False, "reason": "expired",
                                                "filename": "agreement.pdf", "kept_file": vault.rel(kept)}]}))
    db.commit()

    monkeypatch.setattr(rc, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(sys, "argv", ["recheck", "--apply", "--no-model", "--no-message"])
    rc.main()
    assert "ACCEPTED (issued 2025-07-01, valid till 2028-06-01)" in capsys.readouterr().out
    db.expire_all()
    docs = db.query(Document).filter(Document.csp_id == csp.id, Document.document_type == "AGREEMENT").all()
    assert [(d.expiry_date, d.status) for d in docs] == [(date(2028, 6, 1), DocumentStatus.VALID)]


def test_application_receipt_uploaded_in_police_section_is_turned_away_not_reviewed(tmp_path):
    """The upload section can't override wording that says 'application, not the certificate'."""
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(["Acknowledgement Receipt", "Application Form for Character Certificate",
                              "Application No. BCHC/2026/1453186   Date of Application: 14/10/2026",
                              "Name of Applicant: Sonali Priya   District: NAWADA   State: BIHAR",
                              "Service will be delivered within 14 working days."]):
        page.insert_text((40, 60 + 20 * i), line, fontsize=10)
    r = extract_document_fields_deterministic(d.tobytes(), "pvr.pdf", allow_llm_fallback=False,
                                              expected_type="POLICE_VERIFICATION")
    assert r["readability"] == "NOT_ALLOWED" and r["document_type"] == "PVR_APPLICATION_ONLY"
    assert not r.get("type_review")


def test_check_all_documents_lists_a_wrong_stored_expiry_and_changes_nothing(db_session, tmp_path, monkeypatch, capsys):
    import scripts.check_all_documents as chk
    import app.ai.providers.vision_fallback as vf
    monkeypatch.setattr(vf, "read_page", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model asked")))
    db = db_session
    code = f"9K{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Check CSP", current_code=code, lookup_code=code, phone="9876543210",
              whatsapp_number="9876543210", is_active_in_calling_sheet=True)
    db.add(csp)
    db.flush()
    f = tmp_path / "agreement.pdf"
    _pdf(f)
    doc = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, status=DocumentStatus.VALID,
                   issue_date=date(2025, 7, 1), expiry_date=date(2027, 7, 1), is_current=True,
                   readability="READABLE", validity_rule_used="AGREEMENT_DEFAULT_2_YEAR", storage_path=str(f))
    db.add(doc)
    db.commit()
    monkeypatch.setattr(chk, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["check", "--csp", code])
    chk.main()
    out = capsys.readouterr().out
    assert "re-read 2025-07-01 -> 2028-06-01   [DIFFERENT]" in out and "1 DIFFERENT" in out
    assert (tmp_path / "logs" / "document_check.csv").exists()
    db.refresh(doc)
    assert doc.expiry_date == date(2027, 7, 1)


def test_a_file_that_hangs_is_listed_as_timeout_and_the_check_goes_on(tmp_path, monkeypatch):
    import time
    import scripts.check_all_documents as chk
    monkeypatch.setattr(chk, "_read", lambda path, t, q: time.sleep(30))
    f = tmp_path / "a.pdf"
    _pdf(f)
    started = time.time()
    assert chk.read_isolated(f, "AGREEMENT", timeout=1) == {"timeout": True}
    assert time.time() - started < 10
    monkeypatch.undo()
    assert chk.read_isolated(f, "AGREEMENT", timeout=120)["expiry_date"] == "2028-06-01"


def test_eval_reading_scores_against_what_a_person_decided(db_session, tmp_path, monkeypatch, capsys):
    import scripts.eval_reading as ev
    from app.models import ManualReviewQueue, MonitorRun, ReviewStatus
    db = db_session
    code = f"9V{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Eval CSP", current_code=code, lookup_code=code, phone="9876543210",
              whatsapp_number="9876543210", is_active_in_calling_sheet=True)
    db.add(csp)
    db.flush()
    f = tmp_path / "agreement.pdf"
    _pdf(f)
    right = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex,
                     status=DocumentStatus.MANUAL_VERIFIED, issue_date=date(2025, 7, 1), expiry_date=date(2028, 6, 1),
                     is_current=True, readability="READABLE", storage_path=str(f))
    wrong = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex,
                     status=DocumentStatus.MANUAL_VERIFIED, issue_date=date(2025, 7, 1), expiry_date=date(2027, 1, 1),
                     is_current=False, readability="READABLE", storage_path=str(f))
    db.add_all([right, wrong])
    db.flush()
    for d in (right, wrong):
        db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.APPROVED, reason="Spot check",
                                 resolved_at=datetime.now()))
    db.commit()
    monkeypatch.setattr(ev, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.chdir(tmp_path)
    verdicts = {}
    for holdout in (False, True):
        monkeypatch.setattr(sys, "argv", ["eval", "--type", "AGREEMENT"] + (["--holdout"] if holdout else []))
        ev.main()
        for row in csv.DictReader(open(tmp_path / "logs" / "reading_eval.csv")):
            verdicts[int(row["doc_id"])] = row["verdict"]
    capsys.readouterr()
    assert verdicts[right.id] == "correct" and verdicts[wrong.id] == "WRONG ACCEPT"
    assert db.query(MonitorRun).filter(MonitorRun.kind == "reading_eval").count() >= 2
