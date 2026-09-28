"""The two Excel reports, on the test DB (vault folder is a temp dir)."""
import uuid
from datetime import date, timedelta

from openpyxl import load_workbook

from app import reports, vault
from app.models import CSP, Document, DocumentStatus, InternalUser


def _csp(db, **kw):
    code = f"5R{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name=kw.pop("name", "Report Csp"), current_code=code, lookup_code=code,
            is_active_in_calling_sheet=True, **kw)
    db.add(c)
    db.flush()
    return c


def _rows(ws, code):
    return [r for r in ws.iter_rows(values_only=True) if r and r[0] == code]


def test_csp_report_tabs_and_rows(db_session):
    db = db_session
    rm = InternalUser(name=f"RM {uuid.uuid4().hex[:4]}", role="RM")
    db.add(rm)
    db.flush()
    c = _csp(db, rm_id=rm.id, phone="9876543210", category=3)
    db.add(Document(csp_id=c.id, document_type="POLICE_VERIFICATION", sha256=uuid.uuid4().hex,
                    status=DocumentStatus.VALID, readability="READABLE", is_current=True,
                    issue_date=date.today() - timedelta(days=400), expiry_date=date.today() - timedelta(days=35)))
    db.add(Document(csp_id=c.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex,
                    status=DocumentStatus.VALID, readability="READABLE", is_current=True,
                    issue_date=date.today() - timedelta(days=1070), expiry_date=date.today() + timedelta(days=25)))
    db.flush()
    wb = load_workbook(reports.csp_report(db))
    assert wb.sheetnames == ["Summary", "All CSPs", "Cat 1 Active", "Cat 2 Partial", "Cat 3 Expired",
                             "Cat 4 None", "Expiring 60 days"]
    row = _rows(wb["All CSPs"], c.current_code)[0]
    head = [h.value for h in wb["All CSPs"][1]]
    assert row[head.index("PVR status")] == "EXPIRED" and row[head.index("Agreement days left")] == 25
    assert row[head.index("IIBF status")] == "MISSING" and row[head.index("RM")] == rm.name
    assert _rows(wb["Cat 3 Expired"], c.current_code)
    exp = _rows(wb["Expiring 60 days"], c.current_code)
    assert exp and exp[0][4] == "CSP Agreement" and exp[0][7] == 25
    # expired PVR cell is red
    r = next(i for i, x in enumerate(wb["All CSPs"].iter_rows(values_only=True), 1) if x[0] == c.current_code)
    assert wb["All CSPs"].cell(row=r, column=head.index("PVR status") + 1).fill.fgColor.rgb.endswith("FDE2E2")


def test_contacts_report_lists_each_gap(db_session):
    db = db_session
    c = _csp(db, name="=HYPERLINK(bad)", phone=None, email=None,
             contact_gaps={"csp": ["phone_missing", "email_missing"], "rm": ["not_assigned"], "dc": ["not_assigned"]})
    wb = load_workbook(reports.contacts_report(db))
    assert wb.sheetnames == ["Summary", "CSP contact missing", "RM gaps", "DC gaps", "RM & DC directory",
                             "Contact changes from CSPs"]
    row = _rows(wb["CSP contact missing"], c.current_code)[0]
    assert row[1] == "'=HYPERLINK(bad)"                # never runs as a formula
    assert row[5] == "Phone missing, Email missing"
    assert _rows(wb["RM gaps"], c.current_code)[0][5] == "Not assigned"
    assert _rows(wb["DC gaps"], c.current_code)[0][5] == "Not assigned"


def test_daily_files_are_written_and_old_ones_pruned(db_session):
    d = reports.reports_dir()
    d.mkdir(parents=True, exist_ok=True)
    old = d / f"CSP_Report_{(date.today() - timedelta(days=45)).isoformat()}.xlsx"
    old.write_bytes(b"x")
    out = reports.write_daily(db_session)
    assert sorted(out["written"]) == sorted([f"CSP_Report_{date.today().isoformat()}.xlsx",
                                             f"Contacts_Gaps_{date.today().isoformat()}.xlsx"])
    assert not old.exists() and (d / out["written"][0]).exists()
    assert d.parent == vault.ROOT.parent
