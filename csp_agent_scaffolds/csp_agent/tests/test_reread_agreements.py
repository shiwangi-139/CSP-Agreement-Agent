"""scripts/reread_default_agreements.py: an agreement stored with the 1-year
default is re-read with the current rules (dry run first, then --apply)."""
import sys
import uuid
from datetime import date

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
