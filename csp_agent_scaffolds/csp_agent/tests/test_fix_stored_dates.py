"""scripts/fix_stored_dates.py: stored documents read with older rules are
corrected when the new reading is clear, sent to review when it isn't, and
dates a person entered are never touched."""
import sys
import uuid
from datetime import date

import pymupdf
from sqlalchemy.orm import sessionmaker

from app.models import CSP, Document, DocumentStatus, ManualReviewQueue

GURSEWAK = ["CUSTOMER SERVICE POINT AGREEMENT", 'On this day of, 11/01/2024 ("Effective Date"), CSP Name GURSEWAK',
            "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign.",
            "A. Term: This Agreement shall commence on the Effective Date and shall be valid upto 10/01/2027",
            "or until terminated in accordance with Clause I(4)B or Clause III(1)."]


def _doc(db, tmp_path, lines, **kw):
    code = f"9F{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Fix CSP", current_code=code, lookup_code=code, phone="9876543210", whatsapp_number="9876543210",
            is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    f = tmp_path / f"{code}.pdf"
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(lines):
        page.insert_text((30, 60 + 20 * i), line, fontsize=8)
    d.save(f)
    doc = Document(csp_id=c.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, readability="READABLE",
                   is_current=True, storage_path=str(f), **kw)
    db.add(doc)
    db.commit()
    return c, doc


def _run(db, monkeypatch, *args):
    import scripts.fix_stored_dates as s
    monkeypatch.setattr(s, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(sys, "argv", ["fix", *args])
    s.main()


def test_expired_by_an_old_guess_is_corrected(db_session, tmp_path, monkeypatch, capsys):
    db = db_session
    c, doc = _doc(db, tmp_path, GURSEWAK, status=DocumentStatus.EXPIRED, issue_date=date(2024, 1, 11),
                  expiry_date=date(2026, 1, 11), validity_rule_used="AGREEMENT_DEFAULT_2_YEAR")
    _run(db, monkeypatch, "--csp", c.current_code)                       # dry run
    assert "now 2024-01-11 -> 2027-01-10   [corrected]" in capsys.readouterr().out
    db.refresh(doc)
    assert doc.expiry_date == date(2026, 1, 11)
    _run(db, monkeypatch, "--csp", c.current_code, "--apply")
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.expiry_date == date(2027, 1, 10) and doc.status == DocumentStatus.VALID


def test_another_issue_date_goes_to_review_not_corrected(db_session, tmp_path, monkeypatch, capsys):
    db = db_session
    c, doc = _doc(db, tmp_path, GURSEWAK, status=DocumentStatus.VALID, issue_date=date(2023, 5, 2),
                  expiry_date=date(2026, 5, 2), validity_rule_used="AGREEMENT_EXPLICIT_3_YEAR")
    _run(db, monkeypatch, "--csp", c.current_code, "--apply")
    assert "[to review]" in capsys.readouterr().out
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.issue_date == date(2023, 5, 2) and doc.status == DocumentStatus.NEEDS_REVIEW
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id).count() == 1


def test_dates_a_person_entered_are_kept(db_session, tmp_path, monkeypatch, capsys):
    db = db_session
    c, doc = _doc(db, tmp_path, GURSEWAK, status=DocumentStatus.MANUAL_VERIFIED, issue_date=date(2024, 1, 11),
                  expiry_date=date(2027, 1, 11), validity_rule_used="MANUAL_REVIEW_36_MONTHS")
    _run(db, monkeypatch, "--csp", c.current_code, "--apply")
    assert "1 kept" in capsys.readouterr().out
    db.refresh(doc)
    assert doc.expiry_date == date(2027, 1, 11) and doc.status == DocumentStatus.MANUAL_VERIFIED


def test_a_valid_agreement_kept_only_by_a_guess_gets_a_check_but_stays_accepted(db_session, tmp_path, monkeypatch, capsys):
    db = db_session
    lines = ["CUSTOMER SERVICE POINT AGREEMENT", 'On this day of, 11/06/2026 ("Effective Date"), CSP Name X',
             "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign."]
    c, doc = _doc(db, tmp_path, lines, status=DocumentStatus.VALID, issue_date=date(2026, 6, 11),
                  expiry_date=date(2027, 6, 11), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR")
    _run(db, monkeypatch, "--csp", c.current_code, "--apply")
    assert "[check validity]" in capsys.readouterr().out
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.status == DocumentStatus.VALID and doc.is_current
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id).one().reason.startswith("Validity check")


def test_an_agreement_held_only_by_the_old_default_rule_becomes_expired_with_a_check(db_session, tmp_path, monkeypatch, capsys):
    from app.compliance import evaluate
    from app.models import ReviewStatus
    db = db_session
    lines = ["CUSTOMER SERVICE POINT AGREEMENT", 'On this day of, 13/02/2025 ("Effective Date"), CSP Name X']
    c, doc = _doc(db, tmp_path, lines, status=DocumentStatus.NEEDS_REVIEW, issue_date=date(2025, 2, 13),
                  expiry_date=date(2026, 2, 13), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR")
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
        "No validity period or end date was found in the agreement, so the default of 1 year would make it "
        "expired on 13-02-2026. Check the agreement for its term.")))
    db.commit()
    _run(db, monkeypatch, "--csp", c.current_code)                       # dry run: listed, nothing changed
    assert "[1-year rule]" in capsys.readouterr().out
    db.refresh(doc)
    assert doc.status == DocumentStatus.NEEDS_REVIEW
    _run(db, monkeypatch, "--csp", c.current_code, "--apply")
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.status == DocumentStatus.EXPIRED
    q = db.query(ManualReviewQueue).filter_by(document_id=doc.id).one()
    assert q.status == ReviewStatus.PENDING and q.reason.startswith("Validity check")
    s = evaluate(db, db.get(CSP, c.id)).docs["AGREEMENT"]
    assert s.status == "EXPIRED" and not s.in_review                    # the CSP is asked to renew it


def test_agreements_accepted_without_a_written_validity_go_back_to_review(db_session, tmp_path, monkeypatch, capsys):
    from datetime import datetime, timedelta
    from app.compliance import evaluate
    from app.models import ReviewStatus
    import scripts.reopen_accepted_agreements as s
    db = db_session
    c, doc = _doc(db, tmp_path, ["x"], status=DocumentStatus.MANUAL_VERIFIED,
                  issue_date=date(2026, 3, 1), expiry_date=date(2027, 3, 1), validity_rule_used="AGREEMENT_DEFAULT_1_YEAR")
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.APPROVED, reason="Validity check: ...",
                             resolved_at=datetime.utcnow() - timedelta(hours=3), correction_notes="by me"))
    db.commit()
    monkeypatch.setattr(s, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(sys, "argv", ["x", "--csp", c.current_code])
    s.main()
    assert "1 agreement(s) found. Dry run" in capsys.readouterr().out
    db.expire_all()
    assert db.get(Document, doc.id).status == DocumentStatus.MANUAL_VERIFIED
    monkeypatch.setattr(sys, "argv", ["x", "--csp", c.current_code, "--apply"])
    s.main()
    db.expire_all()
    assert db.get(Document, doc.id).status == DocumentStatus.NEEDS_REVIEW
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id, status=ReviewStatus.PENDING).one().reason.startswith("Reopened")
    assert evaluate(db, db.get(CSP, c.id)).docs["AGREEMENT"].in_review


def test_being_checked_documents_missing_from_the_queue_are_added(db_session, tmp_path, monkeypatch, capsys):
    from app.models import ReviewStatus
    import scripts.queue_waiting_documents as s
    db = db_session
    c, doc = _doc(db, tmp_path, ["x"], status=DocumentStatus.NEEDS_APPROVAL, issue_date=date(2025, 8, 19),
                  expiry_date=date(2027, 8, 19), validity_rule_used="AGREEMENT_DEFAULT_2_YEAR")
    monkeypatch.setattr(s, "SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr(sys, "argv", ["x"])
    s.main()
    assert c.current_code in capsys.readouterr().out
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id).count() == 0
    monkeypatch.setattr(sys, "argv", ["x", "--apply"])
    s.main()
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id, status=ReviewStatus.PENDING).count() == 1
    s.main()                                                               # second run adds nothing
    assert db.query(ManualReviewQueue).filter_by(document_id=doc.id).count() == 1
