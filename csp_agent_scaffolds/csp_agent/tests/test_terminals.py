"""Agent 2 - Terminal Extension (app/terminals.py): the terminal report is read
and linked by KO code; the LHO is emailed once, 15 days before expiry, only
when the CSP's documents outlast the terminal and only to a verified address;
the RM is followed up 5 days later if a newer report still shows the old date;
a later date in a report is a renewal, whatever period the LHO chose."""
import io
import uuid
from datetime import date, datetime, timedelta

import pytest

from app import auth, terminals as T
from app.comms import send_gate
from app.models import (CSP, CSPCodeHistory, Document, DocumentStatus, InternalUser, LhoContact, OutboundMessage,
                        OutboundStatus, Terminal, TerminalImport, TerminalRequest)
from tests.test_login import CSRF, PUBLIC, PW, _login, world  # noqa: F401  (fixture)
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

TODAY = date(2026, 11, 13)
HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}


@pytest.fixture()
def agent_on(monkeypatch):
    """Agent 2 switched on (the stored switch stays off for every other test)."""
    s = {**T.DEFAULTS, "on": True}
    monkeypatch.setattr(T, "settings", lambda db: s)
    return s


def _tag():
    return uuid.uuid4().hex[:6].upper()          # terminal IDs are kept in upper case


def _code(prefix, tag):
    return f"{prefix}{int(tag, 16) % 1000000:06d}"


def _csp(s, tag, prefix="5T", circle=None, rm=None, docs_until=date(2028, 1, 1), types=None):
    c = CSP(name=f"Terminal CSP {prefix}{tag}", current_code=_code(prefix, tag), lookup_code=_code(prefix, tag),
            is_active_in_calling_sheet=True, circle=circle, rm_id=rm.id if rm else None)
    s.add(c)
    s.flush()
    for t in (types or ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")):
        s.add(Document(csp_id=c.id, document_type=t, sha256=uuid.uuid4().hex, status=DocumentStatus.VALID,
                       is_current=True, readability="READABLE", issue_date=date(2025, 1, 1), expiry_date=docs_until))
    s.flush()
    return c


def _lho(s, name, emails, cc=()):
    c = LhoContact(name=name, name_key=T.lho_key(name), emails=list(emails), cc=list(cc), active=True,
                   verified_by="test", verified_at=datetime(2026, 11, 1))
    s.add(c)
    s.flush()
    return c


def _report(rows, header=("KO Code", "Terminal ID", "Terminal Expiry Date", "LHO", "CSP Name")):
    lines = [",".join(header)] + [",".join(str(x) for x in r) for r in rows]
    return "\n".join(lines).encode()


# ----------------------------------------------------------------- reading
def test_report_columns_and_dates_are_read_in_their_usual_forms():
    data = _report([["1a850757", "T 001", "30-11-2026", "LHO Patna", "Ram"],
                    ["1A850758", "T002", "2026-12-05", "Patna", "Shyam"],
                    ["1A850759", "T003", "05/12/2026", "", ""],
                    ["1A850760", "T004", "31-Nov-2026", "", ""]],
                   header=("Kiosk Code", "TID", "Mapping Validity", "Circle (LHO)", "Agent Name"))
    rows, cols = T.read_report(data, "report.csv")
    assert cols == {"ko": "Kiosk Code", "terminal": "TID", "expiry": "Mapping Validity", "lho": "Circle (LHO)",
                    "name": "Agent Name"}
    assert [r["ko"] for r in rows] == ["1A850757", "1A850758", "1A850759", "1A850760"]
    assert rows[0]["terminal"] == "T001"
    assert [r["expiry"] for r in rows] == [date(2026, 11, 30), date(2026, 12, 5), date(2026, 12, 5), None]
    assert T.parse_date(46356) == date(2026, 11, 30)                     # an Excel serial date
    assert T.parse_date("2026-11-30 00:00:00") == date(2026, 11, 30)
    assert T.lho_key("LHO Patna") == T.lho_key("patna") == "patna"


def test_an_excel_report_with_a_title_row_is_read():
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["SBI terminal validity report"])
    ws.append(["Sr", "KO ID", "Terminal No", "Validity Date"])
    ws.append([1, "1A850757", "T001", datetime(2026, 11, 30)])
    buf = io.BytesIO()
    wb.save(buf)
    rows, cols = T.read_report(buf.getvalue(), "r.xlsx")
    assert cols["ko"] == "KO ID" and cols["expiry"] == "Validity Date"
    assert rows[0]["expiry"] == date(2026, 11, 30) and rows[0]["terminal"] == "T001"


def test_a_file_without_the_two_columns_is_refused_with_the_reason():
    with pytest.raises(ValueError, match="KO code column and a terminal expiry date column"):
        T.read_report(b"Name,Phone\nRam,999", "x.csv")


# ------------------------------------------------------------------ import
def test_import_links_by_ko_code_old_code_or_leaves_it_for_a_person(db_session):
    s, tag = db_session, _tag()
    a = _csp(s, tag, "5A")
    b = _csp(s, tag, "5B")
    s.add(CSPCodeHistory(csp_id=b.id, code=_code("5O", tag)))             # b's old KO code
    s.commit()
    data = _report([[a.current_code.lower(), f"TA{tag}", "30-11-2026", "LHO Patna", ""],
                    [_code("5O", tag), f"TB{tag}", "01-12-2026", "", ""],
                    [_code("5Z", tag), f"TZ{tag}", "02-12-2026", "", "Unknown CSP"],
                    ["", "", "", "", ""]])
    out = T.import_report(s, data, "r.csv", TODAY, "admin")
    assert (out["rows"], out["linked"], out["not_linked"]) == (3, 2, 1)
    ta = s.query(Terminal).filter_by(terminal_key=f"TA{tag}").one()
    tb = s.query(Terminal).filter_by(terminal_key=f"TB{tag}").one()
    tz = s.query(Terminal).filter_by(terminal_key=f"TZ{tag}").one()
    assert (ta.csp_id, ta.link_state, ta.lho) == (a.id, "LINKED", "LHO Patna")
    assert tb.csp_id == b.id
    assert tz.csp_id is None and tz.link_state == "NOT_LINKED"
    T.link_by_hand(s, tz, a.current_code, "admin")
    assert (tz.csp_id, tz.link_state) == (a.id, "MANUAL")


def test_a_later_date_is_a_renewal_whatever_period_and_an_older_report_changes_nothing(db_session):
    s, tag = db_session, _tag()
    c = _csp(s, tag)
    s.commit()
    T.import_report(s, _report([[c.current_code, f"TR{tag}", "30-11-2026", "", ""]]), "a.csv", TODAY, "admin")
    t = s.query(Terminal).filter_by(terminal_key=f"TR{tag}").one()
    req = TerminalRequest(terminal_pk=t.id, csp_id=c.id, expiry_date=date(2026, 11, 30))
    s.add(req)
    s.commit()
    out = T.import_report(s, _report([[c.current_code, f"TR{tag}", "30-11-2027", "", ""]]), "b.csv",
                          TODAY + timedelta(days=4), "admin")
    assert out["renewed"] == 1
    s.refresh(t)
    s.refresh(req)
    assert t.expiry_date == date(2027, 11, 30)                             # the LHO chose one year
    assert (req.outcome, req.new_expiry_date) == ("RENEWED", date(2027, 11, 30))
    T.import_report(s, _report([[c.current_code, f"TR{tag}", "30-11-2026", "", ""]]), "old.csv", TODAY, "admin")
    s.refresh(t)
    assert t.expiry_date == date(2027, 11, 30)                             # an older report never undoes it
    row = [r for r in T.rows(s, CSP.id == c.id, today=TODAY + timedelta(days=5))][0]
    assert row["renewed"]["to"] == "2027-11-30" and row["renewed"]["months"] == 12


# --------------------------------------------------------------- readiness
def test_ready_only_when_every_document_outlasts_the_terminal():
    v = lambda d: {"status": "VALID", "expiry": d}
    end = date(2026, 11, 30)
    docs = {"AGREEMENT": v(date(2028, 1, 1)), "POLICE_VERIFICATION": v(date(2027, 1, 1)), "IIBF_CERTIFICATE": v(None)}
    assert T.readiness(docs, end, T.REQUIRED_TYPES)[0] is True              # IIBF with no expiry date counts
    docs["POLICE_VERIFICATION"] = v(date(2026, 11, 30))
    ok, why = T.readiness(docs, end, T.REQUIRED_TYPES)
    assert not ok and why == "PVR ends 30-11-2026, before the terminal"
    docs["POLICE_VERIFICATION"] = {"status": "MISSING", "expiry": None}
    assert T.readiness(docs, end, ["AGREEMENT", "IIBF_CERTIFICATE"])[0] is True   # only the documents that count


# ---------------------------------------------------------------- the agent
def _due_terminal(s, tag, days=15, **kw):
    c = _csp(s, tag, **kw)
    t = Terminal(terminal_key=f"TD{tag}", terminal_id=f"TD{tag}", ko_code=c.current_code, csp_id=c.id,
                 link_state="LINKED", lho=f"LHO Test {tag}", expiry_date=TODAY + timedelta(days=days),
                 last_report_date=TODAY)
    s.add(t)
    s.commit()
    return c, t


def test_nothing_is_drafted_while_agent_2_is_off(db_session):
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag)
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    assert "skipped" in T.run(s, today=TODAY)
    assert s.query(OutboundMessage).filter_by(csp_id=c.id).count() == 0


def test_lho_emailed_once_15_days_before_to_the_verified_address_only(db_session, agent_on):
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag)
    _lho(s, f"lho test {tag}", [f"lho{tag}@sbi.co.in"], cc=[f"cc{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY - timedelta(days=1))                              # 16 days before: not yet
    assert s.query(OutboundMessage).filter_by(csp_id=c.id).count() == 0
    T.run(s, today=TODAY)
    T.run(s, today=TODAY)                                                   # a second run drafts nothing more
    msgs = s.query(OutboundMessage).filter_by(csp_id=c.id).all()
    assert len(msgs) == 1
    m = msgs[0]
    assert (m.template_name, m.recipient_role, m.channel, m.destination, m.status) == \
        (T.LHO_TEMPLATE, "LHO", "EMAIL", f"lho{tag}@sbi.co.in", OutboundStatus.QUEUED_FOR_REVIEW)   # as saved
    assert m.payload_json["cc"] == [f"cc{tag}@sbi.co.in"]
    assert c.current_code in m.payload_json["subject"] and "28-11-2026" in m.payload_json["body"]
    req = s.query(TerminalRequest).filter_by(terminal_pk=t.id).one()
    assert req.lho_message_id == m.id and req.documents["AGREEMENT"]["expiry"] == "2028-01-01"
    from app.comms.outbound import recipient_allowed
    assert recipient_allowed(s, c, "LHO", "EMAIL", f"lho{tag}@sbi.co.in")
    assert not recipient_allowed(s, c, "LHO", "EMAIL", "someone@gmail.com")
    assert not recipient_allowed(s, c, "LHO", "WHATSAPP", "9876543210")


def test_documents_ending_before_the_terminal_or_no_lho_address_mean_no_email(db_session, agent_on):
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag, docs_until=TODAY + timedelta(days=10))     # end before the terminal
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY)
    assert s.query(OutboundMessage).filter_by(csp_id=c.id).count() == 0
    row = T.rows(s, CSP.id == c.id, today=TODAY)[0]
    assert row["status"] == "not_ready" and "ends" in row["reason"]
    tag2 = _tag()
    c2, t2 = _due_terminal(s, tag2)                                         # ready, but its LHO has no address
    T.run(s, today=TODAY)
    req = s.query(TerminalRequest).filter_by(terminal_pk=t2.id).one()
    assert req.lho_message_id is None and "No verified email address" in req.blocked_reason
    assert T.rows(s, CSP.id == c2.id, today=TODAY)[0]["status"] == "no_lho"


def test_documents_valid_late_still_get_the_email_once_before_expiry(db_session, agent_on):
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag, days=8, types=("AGREEMENT", "IIBF_CERTIFICATE"))   # PVR missing
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY)
    assert s.query(OutboundMessage).filter_by(csp_id=c.id).count() == 0
    s.add(Document(csp_id=c.id, document_type="POLICE_VERIFICATION", sha256=uuid.uuid4().hex,
                   status=DocumentStatus.VALID, is_current=True, readability="READABLE",
                   issue_date=TODAY, expiry_date=date(2027, 11, 13)))       # Agent 1 accepted it
    s.commit()
    T.run(s, today=TODAY)
    assert s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.LHO_TEMPLATE).count() == 1


def test_rm_followed_up_once_after_5_days_only_on_a_newer_report(db_session, agent_on):
    s, tag = db_session, _tag()
    rm = InternalUser(name=f"RM {tag}", role="RM", email=f"rm{tag}@eko.co.in")
    s.add(rm)
    s.flush()
    c, t = _due_terminal(s, tag, rm=rm)
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY)
    m = s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.LHO_TEMPLATE).one()
    m.status, m.sent_at = OutboundStatus.SENT, datetime(2026, 11, 13, 5, 0)   # 10:30 in India
    s.commit()
    T.run(s, today=TODAY + timedelta(days=5))
    rm_msgs = lambda: s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.RM_TEMPLATE).all()
    assert len(rm_msgs()) == 1                                             # the report of the 13th is on the email's day
    r = rm_msgs()[0]
    assert (r.recipient_role, r.destination) == ("RM", f"rm{tag}@eko.co.in".lower())
    assert "still shows the expiry date 28-11-2026" in r.payload_json["body"]
    T.run(s, today=TODAY + timedelta(days=6))
    assert len(rm_msgs()) == 1                                             # once


def test_no_rm_follow_up_when_the_latest_report_is_older_than_the_email(db_session, agent_on):
    s, tag = db_session, _tag()
    rm = InternalUser(name=f"RM {tag}", role="RM", email=f"rm{tag}@eko.co.in")
    s.add(rm)
    s.flush()
    c, t = _due_terminal(s, tag, rm=rm)
    t.last_report_date = TODAY - timedelta(days=2)
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY)
    m = s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.LHO_TEMPLATE).one()
    m.status, m.sent_at = OutboundStatus.SENT, datetime(2026, 11, 13, 5, 0)
    s.commit()
    T.run(s, today=TODAY + timedelta(days=6))
    assert s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.RM_TEMPLATE).count() == 0


def test_alert_3_days_before_and_expired_requests_close(db_session, agent_on):
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag, days=3, docs_until=TODAY)                  # not ready: no email, still an alert
    s.commit()
    assert T.run(s, today=TODAY)["alerts"] >= 1
    req = s.query(TerminalRequest).filter_by(terminal_pk=t.id).one()
    assert req.alert_at is not None and T.rows(s, CSP.id == c.id, today=TODAY)[0]["alert"]
    T.run(s, today=TODAY + timedelta(days=4))
    s.refresh(req)
    assert req.outcome == "EXPIRED"
    assert T.rows(s, CSP.id == c.id, today=TODAY + timedelta(days=4))[0]["status"] == "expired"


def test_approval_off_approves_the_email_itself(db_session, agent_on):
    agent_on["approval"] = False
    s, tag = db_session, _tag()
    c, t = _due_terminal(s, tag)
    _lho(s, f"LHO Test {tag}", [f"lho{tag}@sbi.co.in"])
    s.commit()
    T.run(s, today=TODAY)
    m = s.query(OutboundMessage).filter_by(csp_id=c.id, template_name=T.LHO_TEMPLATE).one()
    assert m.status == OutboundStatus.APPROVED and m.reviewed_by == "agent 2 (approval off)"


def test_send_gate_holds_lho_emails_while_off_and_never_calls_them_repeats(db_session, monkeypatch):
    s, tag = db_session, _tag()
    c = _csp(s, tag)
    mk = lambda n: OutboundMessage(csp_id=c.id, template_name=T.LHO_TEMPLATE, channel="EMAIL",
                                   destination=f"lho{tag}@sbi.co.in", recipient_role="LHO",
                                   status=OutboundStatus.APPROVED, idempotency_key=f"t{tag}{n}",
                                   sent_at=datetime(2026, 11, 13, 5, 0) if n == 1 else None)
    first, second = mk(1), mk(2)
    first.status = OutboundStatus.SENT
    s.add_all([first, second])
    s.commit()
    monkeypatch.setattr(T, "settings", lambda db: {**T.DEFAULTS, "on": False})
    assert send_gate.decide(s, second)[0] == "wait"
    monkeypatch.setattr(T, "settings", lambda db: {**T.DEFAULTS, "on": True})
    assert send_gate.decide(s, second)[0] == "send"                        # same LHO, another terminal: not a repeat


# --------------------------------------------------------------- the pages
def test_terminals_page_bank_reports_and_excel_for_an_admin(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    tag = _tag()
    x = _csp(s, tag, circle=f"LHO Page {tag}")
    s.commit()
    code, xid = x.current_code, x.id
    s.close()
    data = _report([[code, f"TP{tag}", (TODAY + timedelta(days=10)).strftime("%d-%m-%Y"), "", "x"],
                    [_code("5Q", tag), f"TQ{tag}", "01-12-2026", "", "Nobody"]])
    r = c.post("/api/hub/terminals/import", headers=HDR, files={"file": ("t.csv", data, "text/csv")},
               data={"report_date": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    assert (r.json()["linked"], r.json()["not_linked"]) == (1, 1)
    d = c.get(f"/api/hub/terminals?q={code}", headers=HDR).json()
    assert d["shown"] == 1 and d["rows"][0]["lho"] == f"LHO Page {tag}"          # LHO from the calling sheet
    assert d["rows"][0]["csp"]["code"] == code
    g = c.get(f"/api/hub/terminals?q={tag}&group=lho", headers=HDR).json()
    assert {x["name"] for x in g["groups"]} >= {f"LHO Page {tag}"}
    br = c.get("/api/hub/bank-reports", headers=HDR).json()
    assert any(t["ko_code"] == _code("5Q", tag) for t in br["not_linked"])
    assert any(l["name"] == f"LHO Page {tag}" and not l["emails"] for l in br["lhos"])
    bad = c.post("/api/hub/lho-contacts", headers=HDR, json={"name": f"LHO Page {tag}", "emails": ["not-an-email"]})
    assert bad.status_code == 422
    ok = c.post("/api/hub/lho-contacts", headers=HDR, json={"name": f"LHO Page {tag}", "emails": [f"P{tag}@SBI.co.in"]})
    assert ok.status_code == 200 and ok.json()["emails"] == [f"p{tag}@sbi.co.in".lower()]
    xl = c.get(f"/api/hub/terminals.xlsx?q={code}", headers=HDR)
    assert xl.status_code == 200 and xl.content[:2] == b"PK"
    assert c.post("/api/hub/terminals/import", headers=HDR, files={"file": ("x.csv", b"a,b\n1,2", "text/csv")},
                  data={"report_date": TODAY.isoformat()}).status_code == 422
    csp = c.get(f"/api/hub/csp/{xid}", headers=HDR).json()
    assert csp["terminals"][0]["terminal_id"] == f"TP{tag}"


def test_an_rm_sees_only_their_terminals_and_cannot_approve_an_lho_email(world):
    c, w = world
    from app.db import get_db
    from app.main import app
    gen = app.dependency_overrides[get_db]()
    s = next(gen)
    tag = _tag()
    mine_id, mine_code = w["mine"]
    theirs_id, theirs_code = w["theirs"]
    for cid, code in ((mine_id, mine_code), (theirs_id, theirs_code)):
        s.add(Terminal(terminal_key=f"W{code}", terminal_id=f"W{code}", ko_code=code, csp_id=cid, link_state="LINKED",
                       expiry_date=TODAY + timedelta(days=20)))
    m = OutboundMessage(csp_id=mine_id, template_name=T.LHO_TEMPLATE, channel="EMAIL", destination="x@sbi.co.in",
                        recipient_role="LHO", status=OutboundStatus.QUEUED_FOR_REVIEW, idempotency_key=f"rmtest{tag}")
    s.add(m)
    s.commit()
    mid = m.id
    assert _login(c, w["rm"]).status_code == 200
    d = c.get("/api/hub/terminals", headers={**PUBLIC, **CSRF}).json()
    codes = {r["ko_code"] for r in d["rows"]}
    assert mine_code in codes and theirs_code not in codes
    assert c.get("/api/hub/bank-reports", headers={**PUBLIC, **CSRF}).status_code == 403
    assert c.post(f"/api/hub/messages/{mid}/approve", headers={**PUBLIC, **CSRF}).status_code == 403
