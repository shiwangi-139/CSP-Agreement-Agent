"""scripts/import_contact_numbers.py: extra numbers kept apart from the calling sheet."""
import sys
import uuid

from sqlalchemy.orm import sessionmaker

from app.models import CSP, CspExtraPhone
from scripts import import_contact_numbers as imp


def test_import_adds_new_numbers_skips_known_shared_and_ignores_email(test_engine, tmp_path, monkeypatch):
    Session = sessionmaker(bind=test_engine)
    monkeypatch.setattr(imp, "SessionLocal", Session)
    monkeypatch.chdir(tmp_path)
    s = Session()
    t = uuid.uuid4().int % 1000000
    a = CSP(name="Kaushar Jahan", current_code=f"2K{t:06d}", lookup_code=f"2K{t:06d}", phone="9876501111",
            is_active_in_calling_sheet=True)
    b = CSP(name="Other", current_code=f"2L{t:06d}", lookup_code=f"2L{t:06d}", is_active_in_calling_sheet=True)
    s.add_all([a, b])
    s.commit()
    a_id, a_code, b_code = a.id, a.current_code, b.current_code
    s.close()
    sheet = tmp_path / "contacts.csv"
    sheet.write_text(
        "CSP Code,Full Name,Mobile Number,Home Phone,Work Phone,Email\n"
        f"{a_code},KAUSHAR JAHAN,7838243641,7838243641,9876501111,wrong@example.com\n"   # new · same again · on sheet
        f"{b_code},OTHER,9123400000,,,x@y.z\n"                                         # shared with the next row
        f"{a_code},KAUSHAR JAHAN,9123400000,,,\n"
        "9Z999999,NOBODY,9000000001,,,\n"                                              # CSP not found
        f"{b_code},OTHER,12345,,,\n")                                                  # invalid
    monkeypatch.setattr(sys, "argv", ["x", str(sheet), "--apply"])
    imp.main()
    s = Session()
    added = {(e.csp_id, e.phone) for e in s.query(CspExtraPhone)}
    assert (a_id, "7838243641") in added                     # new, added once
    assert not any(p == "9123400000" for _, p in added)      # shared between two CSPs: not added
    assert not any(p == "9876501111" for _, p in added)      # already on the calling sheet
    assert s.get(CSP, a_id).phone == "9876501111"            # calling sheet untouched
    s.close()
    report = next((tmp_path / "logs").glob("contact_import_*.csv")).read_text()
    assert "SHARED" in report and "CSP_NOT_FOUND" in report and "INVALID" in report and "@" not in report
