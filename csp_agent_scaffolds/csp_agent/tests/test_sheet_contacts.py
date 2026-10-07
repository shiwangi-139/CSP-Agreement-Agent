"""Calling sheet: RM contact columns ("Mobile No Of RM", "Email of RM") fill
the RM's phone and email, like the DC columns do (app/comms/sheets_sync.py)."""
import uuid

from app.comms.sheets_sync import _upsert_staff, parse_row
from app.models import InternalUser


def _raw(rm, phone="98765 43210", email="RM.One@Eko.co.in"):
    return {"CSP ID ": "1A850684", "CSP Name": "Ram", "CSP Mobile number": "9876500000",
            "Relationship Manager": rm, "Mobile No Of RM": phone, "Email of RM": email,
            "District Coordinator": "DC X", "Mobile Number DC": "9000000002", "Email ID DC": "dc@eko.co.in"}


def test_rm_columns_are_read():
    r = parse_row(_raw("Amuda"))
    assert r["rm_phone"] == "9876543210" and r["rm_email"] == "rm.one@eko.co.in"


def test_rm_gets_the_most_common_contact_and_blanks_never_erase(db_session):
    name = f"RM {uuid.uuid4().hex[:6]}"
    rows = [parse_row(_raw(name)), parse_row(_raw(name)), parse_row(_raw(name, phone="9111111111", email=""))]
    email = f"{uuid.uuid4().hex[:6]}@eko.co.in"
    for r in rows[:2]:
        r["rm_email"] = email
    rm = _upsert_staff(db_session, "RM", {name: rows})[name.lower()]
    assert rm.phone == "9876543210" and rm.email == email
    _upsert_staff(db_session, "RM", {name: [parse_row(_raw(name, phone="", email=""))]})
    assert rm.phone == "9876543210" and rm.email == email          # blank sheet cells keep what we had


def test_sheet_does_not_change_the_login_email_of_an_rm_who_can_log_in(db_session):
    name = f"RM {uuid.uuid4().hex[:6]}"
    login = f"login{uuid.uuid4().hex[:6]}@eko.co.in"
    db_session.add(InternalUser(name=name, role="RM", email=login, login_enabled=True))
    db_session.flush()
    r = parse_row(_raw(name, email=f"other{uuid.uuid4().hex[:6]}@eko.co.in"))
    rm = _upsert_staff(db_session, "RM", {name: [r]})[name.lower()]
    assert rm.email == login and rm.phone == "9876543210"
