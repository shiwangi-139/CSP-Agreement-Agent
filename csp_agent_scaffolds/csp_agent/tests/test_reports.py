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
    assert wb.sheetnames == ["Summary", "All CSPs", "Label 1 All documents on file", "Label 2 1 document missing",
                             "Label 3 2 documents missing", "Label 4 No documents", "Renewal due", "Expiring 60 days"]
    row = _rows(wb["All CSPs"], c.current_code)[0]
    head = [h.value for h in wb["All CSPs"][1]]
    assert row[head.index("PVR status")] == "EXPIRED" and row[head.index("Agreement days left")] == 25
    assert row[head.index("IIBF status")] == "MISSING" and row[head.index("RM")] == rm.name
    assert _rows(wb["Label 2 1 document missing"], c.current_code)      # 2 of 3 on file
    assert _rows(wb["Renewal due"], c.current_code)                     # its PVR is expired
    exp = _rows(wb["Expiring 60 days"], c.current_code)
    assert exp and exp[0][4] == "CSP Agreement" and exp[0][7] == 25
    # expired PVR cell is red
    r = next(i for i, x in enumerate(wb["All CSPs"].iter_rows(values_only=True), 1) if x[0] == c.current_code)
    assert wb["All CSPs"].cell(row=r, column=head.index("PVR status") + 1).fill.fgColor.rgb.endswith("FDE2E2")


def test_gaps_report_lists_each_gap(db_session):
    db = db_session
    c = _csp(db, name="=HYPERLINK(bad)", phone=None, email=None,
             contact_gaps={"csp": ["phone_missing", "email_missing"], "rm": ["not_assigned"], "dc": ["not_assigned"]})
    wb = load_workbook(reports.gaps_report(db))
    assert wb.sheetnames == ["Summary", "CSP contact missing", "RM gaps", "DC gaps", "RM & DC missing details",
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
                                             f"Contacts_{date.today().isoformat()}.xlsx",
                                             f"Contact_Gaps_{date.today().isoformat()}.xlsx"])
    assert not old.exists() and (d / out["written"][0]).exists()
    assert d.parent == vault.ROOT.parent


def test_contacts_report_lists_every_csp_with_rm_and_dc(db_session):
    db = db_session
    c = _csp(db, phone="9876500000", email=None)
    wb = load_workbook(reports.contacts_report(db))
    assert wb.sheetnames == ["All CSP contacts", "RM & DC directory"]
    ws = wb["All CSP contacts"]
    head = [h.value for h in ws[1]]
    assert head[:6] == ["CSP code", "CSP name", "Phone", "Alt phone", "WhatsApp", "Email"]
    row = _rows(ws, c.current_code)[0]
    assert row[head.index("Phone")] == "9876500000" and row[head.index("Email")] is None
    r = next(i for i, x in enumerate(ws.iter_rows(values_only=True), 1) if x[0] == c.current_code)
    assert ws.cell(row=r, column=head.index("Email") + 1).fill.fgColor.rgb.endswith("FDE2E2")   # empty = red


def test_unmatched_emails_are_sorted_and_list_their_codes(db_session):
    from app.models import InboundMessage
    db = db_session
    tag = uuid.uuid4().hex[:6]
    rows = [("Request to terminal extension for KO 1A859991", "code seen: 1A859991, name seen: Mubeen Nasir"),
            ("Request for terminal extension 1A859992", "code seen: none, name seen: none"),     # code only in subject
            ("BC-CSP AGREEMENT & PVR PENDENCY REPORT AS ON 21.06.2026", "code seen: none, name seen: none"),
            ("Issue in Uploading CSP PVR and Agreement", "code seen: none, name seen: none")]
    for subject, note in rows:
        db.add(InboundMessage(external_message_id=f"u{tag}{len(subject)}", subject=subject, sender="x@eko.co.in",
                              status="UNMATCHED_NO_CSP", error_message=f"No CSP on the calling sheet matches this email ({note})."))
    db.flush()
    u = reports.unmatched_emails(db)
    codes = {e["code"]: e for e in u["codes"]}
    assert "1A859991" in codes and codes["1A859991"]["names"] == ["Mubeen Nasir"]
    assert "1A859992" in codes                                    # found in the subject
    kinds = {e["subject"]: e["kind"] for e in u["emails"]}
    assert kinds["BC-CSP AGREEMENT & PVR PENDENCY REPORT AS ON 21.06.2026"] == "report"
    assert kinds["Issue in Uploading CSP PVR and Agreement"] == "no_code"
    from openpyxl import load_workbook
    assert load_workbook(reports.unmatched_report(db)).sheetnames == ["Summary", "Codes not on calling sheet",
                                                                      "All unmatched emails"]
