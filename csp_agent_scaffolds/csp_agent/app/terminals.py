"""
app/terminals.py
Agent 2 - Terminal Extension: no CSP's terminal expires.

A terminal's validity is its own date, set by SBI's LHO; it is not the
validity of the CSP's documents. The agent:

  1. reads the terminal expiry report an admin uploads (Bank reports page),
     links every terminal to its CSP by KO code (old KO codes too; never a
     guess) and keeps every expiry date a terminal ever had;
  2. checks the CSP's documents (Agent 1) against the terminal's expiry date:
     ready = every required document is valid beyond it;
  3. LEAD_DAYS (15) before expiry, when ready: ONE email to the LHO, from
     Eko's mailbox, only to the LHO's verified address (lho_contacts); a
     person approves it while settings["approval"] is on (first 2 weeks);
  4. RM_AFTER_DAYS (5) after that email, if a report dated on or after the
     email still shows the old date: ONE email to the CSP's RM;
  5. ALERT_DAYS (3) before expiry and not renewed: on the admins' list
     (Today, Terminals);
  6. renewed = a later report shows a later date. The new date is taken from
     the report as it is: the LHO decides the period, never the documents.

A CSP whose documents are not ready gets nothing new: Agent 1 is already
reminding them, and the terminal is checked again every day.
Nothing is sent while Agent 2 is switched off (the default).
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .compliance import REQUIRED_TYPES, USABLE, canonical_type, evaluate, is_inactive
from .models import (CSP, CSPCodeHistory, Document, DocumentStatus, IngestState, InternalUser, LhoContact,
                     ManualReviewQueue, OutboundMessage, OutboundStatus, ReviewStatus, Terminal, TerminalExpiry,
                     TerminalImport, TerminalRequest)

logger = logging.getLogger(__name__)

SETTINGS_KEY = "agent2_terminal_extension"
DEFAULTS = {"on": False, "approval": True, "lead_days": 15, "rm_after_days": 5, "alert_days": 3,
            "required": list(REQUIRED_TYPES)}
LHO_TEMPLATE, RM_TEMPLATE = "TERMINAL_EXTENSION_LHO", "TERMINAL_EXTENSION_RM"
TEMPLATES = (LHO_TEMPLATE, RM_TEMPLATE)
SHORT = {"AGREEMENT": "Agreement", "POLICE_VERIFICATION": "PVR", "IIBF_CERTIFICATE": "IIBF"}
LONG = {"AGREEMENT": "CSP Agreement", "POLICE_VERIFICATION": "Police verification / character certificate",
        "IIBF_CERTIFICATE": "IIBF certificate"}
# Days-left groups on the Terminals page, in order.
BUCKETS = (("expired", "Expired"), ("d15", "0–15 days"), ("d30", "16–30 days"), ("d60", "31–60 days"),
           ("d90", "61–90 days"), ("later", "Later"), ("nodate", "No date"))
# One status per terminal, for its chip and the filters (most urgent first).
STATUSES = {
    "expired": "Expired, not renewed",
    "rm": "RM followed up",
    "emailed": "LHO emailed",
    "approval": "Email waiting for approval",          # to the LHO, or the RM follow-up
    "failed": "LHO email failed or blocked",
    "rejected": "LHO email rejected by a person",
    "no_lho": "No LHO email address",
    "not_ready": "Documents not ready",
    "due": "Ready, email due",
    "ready": "Ready",
    "not_linked": "Not linked to a CSP",
}
RENEWED_RECENT_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_today() -> date:
    from .config import local_now
    return local_now().date()


def fmt(d: Optional[date]) -> str:
    return d.strftime("%d-%m-%Y") if d else "—"


# ------------------------------------------------------------------ settings
def settings(db: Session) -> dict:
    st = db.get(IngestState, SETTINGS_KEY)
    return {**DEFAULTS, **((st.value or {}) if st is not None else {})}


def set_settings(db: Session, changes: dict, by: str) -> dict:
    cur = settings(db)
    for k in ("on", "approval"):
        if k in changes and changes[k] is not None:
            cur[k] = bool(changes[k])
    for k, lo, hi in (("lead_days", 1, 90), ("rm_after_days", 1, 30), ("alert_days", 0, 30)):
        if changes.get(k) is not None:
            v = int(changes[k])
            if not lo <= v <= hi:
                raise ValueError(f"{k} must be between {lo} and {hi}")
            cur[k] = v
    if changes.get("required") is not None:
        req = [t for t in changes["required"] if t in REQUIRED_TYPES]
        if not req:
            raise ValueError("At least one document must count.")
        cur["required"] = req
    cur.update({"changed_by": by, "changed_at": _now().isoformat(timespec="minutes")})
    st = db.get(IngestState, SETTINGS_KEY)
    if st is None:
        st = IngestState(key=SETTINGS_KEY, value={})
        db.add(st)
    st.value = cur
    db.commit()
    return cur


# ------------------------------------------------------------ reading files
HEADERS = {
    "ko": ("ko code", "ko id", "kocode", "koid", "ko no", "kiosk code", "kiosk id", "csp code", "csp id",
           "agent code", "agent id", "bc code", "bca code", "bc agent code", "ko"),
    "terminal": ("terminal id", "terminal", "tid", "terminal no", "terminal number", "terminal code", "device id",
                 "pos id", "machine id"),
    "expiry": ("terminal expiry date", "terminal expiry", "terminal validity", "terminal validity date",
               "mapping validity", "mapping validity date", "mapping expiry", "mapping expiry date", "expiry date",
               "expiry", "expiry dt", "exp date", "valid till", "valid upto", "valid up to", "validity",
               "validity date", "validity upto", "valid to", "end date", "mapping end date"),
    "lho": ("lho", "lho name", "circle", "circle lho", "lho circle"),
    "branch": ("branch", "branch name", "link branch", "linked branch", "base branch", "home branch"),
    "name": ("csp name", "ko name", "agent name", "bc name", "name"),
}


def _norm_header(v) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).split())


def _columns(header: list) -> dict[str, int]:
    """Which column holds what. Exact names first, then a looser match for
    the expiry date ('... validity ...', '... expiry ...')."""
    names = [_norm_header(h) for h in header]
    found: dict[str, int] = {}
    for key, options in HEADERS.items():
        for opt in options:
            if opt in names and names.index(opt) not in found.values():
                found[key] = names.index(opt)
                break
    if "expiry" not in found:
        for i, n in enumerate(names):
            if i not in found.values() and re.search(r"\b(expir\w*|validity|valid)\b", n):
                found["expiry"] = i
                break
    if "ko" not in found:
        for i, n in enumerate(names):
            if i not in found.values() and re.search(r"\bko\b", n):
                found["ko"] = i
                break
    return found


def parse_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        n = float(v)
        if 20000 < n < 80000:                                    # an Excel serial date
            return date(1899, 12, 30) + timedelta(days=int(n))
        return None
    s = str(v).strip()
    s = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?(\.\d+)?$", "", s)   # "2026-11-30 00:00:00"
    for f in ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y",
              "%d %B %Y", "%d-%b-%y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y", "%b %d, %Y", "%d%m%Y"):
        try:
            d = datetime.strptime(s, f).date()
            return d if 2000 <= d.year <= 2100 else None
        except ValueError:
            continue
    return None


def norm_code(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", "", str(v)).upper()


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _table(data: bytes, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        best: list[list] = []
        for ws in wb.worksheets:                      # the sheet whose header names the columns we need
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            for r in rows[:15]:
                cols = _columns(r)
                if "ko" in cols and "expiry" in cols:
                    return rows
            best = best or rows
        return best
    if name.endswith(".xls"):
        raise ValueError("Old Excel (.xls) files can't be read: open it and save it as .xlsx or .csv.")
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def read_report(data: bytes, filename: str) -> tuple[list[dict], dict]:
    """The report's rows as {ko, terminal, expiry, lho, branch, name, row} and
    which column was read as what. Raises ValueError with a plain reason."""
    rows = _table(data, filename)
    for i, r in enumerate(rows[:15]):
        cols = _columns(r)
        if "ko" in cols and "expiry" in cols:
            header = [_cell(h) for h in r]
            out = []
            for n, raw in enumerate(rows[i + 1:], start=i + 2):
                get = lambda k: raw[cols[k]] if k in cols and cols[k] < len(raw) else None
                if not any(_cell(x) for x in raw):
                    continue
                out.append({"row": n, "ko": norm_code(get("ko")), "terminal": norm_code(get("terminal")),
                            "expiry_raw": _cell(get("expiry")), "expiry": parse_date(get("expiry")),
                            "lho": _cell(get("lho")), "branch": _cell(get("branch")), "name": _cell(get("name"))})
            return out, {k: header[c] for k, c in cols.items()}
    seen = ", ".join(_cell(h) for h in (rows[0] if rows else []) if _cell(h))[:300]
    raise ValueError("Couldn't find a KO code column and a terminal expiry date column in the first 15 rows"
                     + (f" (first row: {seen})" if seen else "") + ".")


# ------------------------------------------------------------------ linking
def lho_key(name: Optional[str]) -> str:
    k = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return re.sub(r"^(lho|circle)", "", k) or k


def link(db: Session, t: Terminal) -> None:
    """Link a terminal to its CSP by KO code: the CSP's code, else an old code
    (KO code history). Two CSPs with that code: left for a person."""
    if t.link_state == "MANUAL":
        return
    code = norm_code(t.ko_code)
    ids: set[int] = set()
    if code:
        ids = {i for (i,) in db.query(CSP.id).filter(or_(func.upper(CSP.current_code) == code,
                                                          func.upper(CSP.lookup_code) == code))}
        if not ids:
            ids = {i for (i,) in db.query(CSPCodeHistory.csp_id).filter(func.upper(CSPCodeHistory.code) == code)}
    if len(ids) == 1:
        new = ids.pop()
        if t.csp_id != new:
            t.csp_id, t.linked_at = new, _now()
        t.link_state, t.linked_by = "LINKED", "KO code"
    else:
        t.csp_id, t.link_state, t.linked_by = None, ("TWO_CSPS" if ids else "NOT_LINKED"), None


def link_by_hand(db: Session, t: Terminal, code: str, by: str) -> CSP:
    code = norm_code(code)
    c = (db.query(CSP).filter(or_(func.upper(CSP.current_code) == code, func.upper(CSP.lookup_code) == code)).first()
         if code else None)
    if c is None:
        raise ValueError(f"No CSP with KO code {code or '(empty)'}.")
    t.csp_id, t.link_state, t.linked_by, t.linked_at = c.id, "MANUAL", by, _now()
    db.commit()
    return c


# ------------------------------------------------------------------- import
def import_report(db: Session, data: bytes, filename: str, report_date: date, by: str) -> dict:
    """Read one terminal report: new terminals, changed expiry dates (a later
    date closes the open request as renewed), links. Returns what happened."""
    rows, columns = read_report(data, filename)
    imp = TerminalImport(file_name=(filename or "")[:200], sha256=hashlib.sha256(data).hexdigest(),
                         report_date=report_date, uploaded_by=by, columns=columns)
    db.add(imp)
    db.flush()
    problems, counts = [], {"read": 0, "new": 0, "renewed": 0, "changed": 0, "skipped": 0}
    latest: dict[str, dict] = {}
    for r in rows:
        if not r["ko"] and not r["terminal"]:
            counts["skipped"] += 1
            continue
        if r["expiry"] is None:
            counts["skipped"] += 1
            if len(problems) < 50:
                problems.append({"row": r["row"], "ko": r["ko"], "reason": f"expiry date not readable: '{r['expiry_raw']}'"})
            continue
        key = r["terminal"] or f"KO:{r['ko']}"
        if key in latest and latest[key]["expiry"] >= r["expiry"]:
            continue                                  # the same terminal twice: the later date counts
        latest[key] = r
    for key, r in latest.items():
        counts["read"] += 1
        t = db.query(Terminal).filter(Terminal.terminal_key == key).first()
        if t is None:
            t = Terminal(terminal_key=key, terminal_id=r["terminal"] or None, first_import_id=imp.id)
            db.add(t)
            counts["new"] += 1
        t.ko_code = r["ko"] or t.ko_code
        t.lho = r["lho"] or t.lho
        t.branch = r["branch"] or t.branch
        t.name_in_report = r["name"] or t.name_in_report
        if t.last_report_date is None or report_date >= t.last_report_date:
            t.last_import_id, t.last_report_date = imp.id, report_date
        link(db, t)
        db.flush()
        if t.expiry_date != r["expiry"] and (t.expiry_report_date is None or report_date >= t.expiry_report_date):
            old = t.expiry_date
            db.add(TerminalExpiry(terminal_pk=t.id, expiry_date=r["expiry"], previous_expiry_date=old,
                                  report_date=report_date, import_id=imp.id))
            t.expiry_date, t.expiry_report_date = r["expiry"], report_date
            if old is not None:
                counts["changed"] += 1
                if r["expiry"] > old:
                    counts["renewed"] += 1
                    for req in db.query(TerminalRequest).filter(TerminalRequest.terminal_pk == t.id,
                                                                TerminalRequest.outcome == "OPEN",
                                                                TerminalRequest.expiry_date < r["expiry"]):
                        req.outcome, req.new_expiry_date, req.closed_at = "RENEWED", r["expiry"], _now()
        elif t.expiry_report_date is None:
            t.expiry_report_date = report_date
    terms = db.query(Terminal).filter(Terminal.last_import_id == imp.id)
    imp.rows_read = counts["read"]
    imp.rows_linked = terms.filter(Terminal.csp_id.isnot(None)).count()
    imp.rows_not_linked = counts["read"] - imp.rows_linked
    imp.rows_skipped, imp.renewed, imp.problems = counts["skipped"], counts["renewed"], problems
    db.commit()
    return {"import_id": imp.id, "report_date": report_date.isoformat(), "columns": columns,
            "rows": counts["read"], "new": counts["new"], "linked": imp.rows_linked, "not_linked": imp.rows_not_linked,
            "skipped": counts["skipped"], "changed": counts["changed"], "renewed": counts["renewed"],
            "problems": problems[:20]}


# ------------------------------------------------------------- the documents
def document_dates(db: Session, csp_ids: Iterable[int], today: Optional[date] = None) -> dict[int, dict]:
    """For many CSPs at once: per required type, its status (VALID, EXPIRED,
    MISSING, IN_REVIEW) and expiry date, as Agent 1 decides it
    (compliance.latest_documents / evaluate)."""
    today = today or local_today()
    ids = list(set(csp_ids))
    out = {i: {t: {"status": "MISSING", "expiry": None} for t in REQUIRED_TYPES} for i in ids}
    if not ids:
        return out
    best: dict[tuple, Document] = {}
    for d in db.query(Document).filter(Document.csp_id.in_(ids), Document.is_current.is_(True)):
        t = canonical_type(d.document_type)
        if t not in REQUIRED_TYPES or d.status not in USABLE or d.readability == "UNREADABLE":
            continue
        k = (d.csp_id, t)
        key = (d.issue_date or date.min, d.uploaded_at or datetime.min)
        b = best.get(k)
        if b is None or key > (b.issue_date or date.min, b.uploaded_at or datetime.min):
            best[k] = d
    waiting = {(c, canonical_type(t)) for c, t in db.query(Document.csp_id, Document.document_type)
               .join(ManualReviewQueue, ManualReviewQueue.document_id == Document.id)
               .filter(Document.csp_id.in_(ids), ManualReviewQueue.status == ReviewStatus.PENDING,
                       Document.status == DocumentStatus.NEEDS_REVIEW)}
    for (c, t), d in best.items():
        expired = d.expiry_date is not None and d.expiry_date < today
        review = d.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.NEEDS_APPROVAL) or (expired and (c, t) in waiting)
        out[c][t] = {"status": "IN_REVIEW" if review else "EXPIRED" if expired else "VALID", "expiry": d.expiry_date}
    return out


def readiness(docs: dict, expiry: Optional[date], required: Iterable[str]) -> tuple[bool, str]:
    """Ready = every required document valid beyond the terminal's expiry date
    (a document with no expiry date, e.g. IIBF, counts as valid)."""
    if expiry is None:
        return False, "No terminal expiry date"
    problems = []
    for t in required:
        s = docs.get(t) or {"status": "MISSING", "expiry": None}
        label = SHORT[t]
        if s["status"] == "MISSING":
            problems.append(f"{label} not on file")
        elif s["status"] == "IN_REVIEW":
            problems.append(f"{label} being checked")
        elif s["status"] == "EXPIRED":
            problems.append(f"{label} expired {fmt(s['expiry'])}")
        elif s["expiry"] is not None and s["expiry"] <= expiry:
            problems.append(f"{label} ends {fmt(s['expiry'])}, before the terminal")
    return (not problems), ("; ".join(problems) if problems else f"All documents valid beyond {fmt(expiry)}")


def _exact_docs(db: Session, csp: CSP) -> dict:
    """The same as document_dates, from Agent 1's own evaluate(): used just
    before an email is drafted."""
    st = evaluate(db, csp)
    return {t: {"status": "IN_REVIEW" if s.in_review else s.status if s.status in ("VALID", "EXPIRED") else "MISSING",
                "expiry": s.expiry_date} for t, s in st.docs.items()}


# --------------------------------------------------------------- the emails
def lho_name(t: Terminal, csp: Optional[CSP]) -> str:
    return (t.lho or (csp.circle if csp else "") or "").strip()


def lho_contact(db: Session, name: str) -> Optional[LhoContact]:
    k = lho_key(name)
    if not k:
        return None
    c = db.query(LhoContact).filter(LhoContact.name_key == k).first()
    return c if c is not None and c.active and c.verified_at and (c.emails or []) else None


def lho_emails_for_csp(db: Session, csp: CSP) -> list[str]:
    """The recipient guard's list for an LHO email about this CSP: the verified
    addresses (To and CC) of the LHOs of its terminals."""
    out: list[str] = []
    for t in db.query(Terminal).filter(Terminal.csp_id == csp.id):
        c = lho_contact(db, lho_name(t, csp))
        if c is not None:
            out += list(c.emails or []) + list(c.cc or [])
    return out


def _doc_line(t: str, s: dict) -> str:
    if s["status"] != "VALID":
        return f"- {LONG[t]}: {s['status'].replace('_', ' ').lower()}"
    return f"- {LONG[t]}: valid till {fmt(s['expiry'])}" if s["expiry"] else f"- {LONG[t]}: valid (no expiry date)"


def lho_email_text(t: Terminal, csp: CSP, docs: dict, required: Iterable[str]) -> dict:
    subject = (f"Terminal extension request: {csp.name}, KO {csp.current_code}"
               + (f", terminal {t.terminal_id}" if t.terminal_id else "") + f" (expires {fmt(t.expiry_date)})")
    lines = [
        "Dear Sir / Madam,", "",
        f"Please extend the validity of the CSP terminal below, which expires on {fmt(t.expiry_date)}.", "",
        f"CSP name: {csp.name}",
        f"KO code: {csp.current_code}",
        *([f"Terminal ID: {t.terminal_id}"] if t.terminal_id else []),
        *([f"Branch: {t.branch or csp.branch}"] if (t.branch or csp.branch) else []),
        f"Terminal expiry date: {fmt(t.expiry_date)}", "",
        "The CSP's documents are valid beyond this date:",
        *[_doc_line(x, docs[x]) for x in required], "",
        f"Kindly extend the terminal before {fmt(t.expiry_date)} so that the CSP's service to customers "
        "is not interrupted. The documents can be shared on request.", "",
        "Regards,", "Eko India Financial Services", "CSP Support",
    ]
    return {"subject": subject, "body": "\n".join(lines)}


def rm_email_text(t: Terminal, csp: CSP, rm: InternalUser, lho: str, emailed_on: date, today: date) -> dict:
    days = (t.expiry_date - today).days
    subject = f"Please follow up with the LHO: terminal of {csp.name} (KO {csp.current_code}) expires {fmt(t.expiry_date)}"
    body = "\n".join([
        f"Dear {rm.name},", "",
        f"We emailed {lho or 'the LHO'} on {fmt(emailed_on)} to extend the terminal of {csp.name} "
        f"(KO {csp.current_code}{', terminal ' + t.terminal_id if t.terminal_id else ''}).",
        f"The terminal report of {fmt(t.last_report_date)} still shows the expiry date {fmt(t.expiry_date)} "
        f"({days} day{'s' if days != 1 else ''} left).", "",
        "Please follow up with the LHO so that the terminal is extended before it expires. "
        "All the CSP's documents are valid beyond this date.", "",
        "— Eko Anupaalan (Agent 2, terminal extension)",
    ])
    return {"subject": subject, "body": body}


def _add_message(db: Session, msg: OutboundMessage) -> OutboundMessage:
    try:
        with db.begin_nested():
            db.add(msg)
    except IntegrityError:
        msg = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == msg.idempotency_key).first()
    return msg


def _draft_lho(db: Session, t: Terminal, csp: CSP, req: TerminalRequest, docs: dict, s: dict) -> Optional[OutboundMessage]:
    name = lho_name(t, csp)
    c = lho_contact(db, name)
    if c is None:
        req.blocked_reason = (f"No verified email address for LHO '{name}'" if name
                              else "The terminal has no LHO (report or calling sheet)")
        return None
    text = lho_email_text(t, csp, docs, s["required"])
    msg = OutboundMessage(csp_id=csp.id, template_name=LHO_TEMPLATE, channel="EMAIL", destination=c.emails[0],
                          payload_json={**text, "cc": list(c.cc or []) + list(c.emails[1:]), "terminal": t.id,
                                        "lho": c.name, "expiry_date": t.expiry_date.isoformat()},
                          idempotency_key=f"agent2:lho:{t.id}:{t.expiry_date.isoformat()}", stage="TERMINAL_EXTENSION",
                          recipient_role="LHO", status=OutboundStatus.QUEUED_FOR_REVIEW, attempts=0, created_at=_now())
    msg = _add_message(db, msg)
    req.blocked_reason, req.lho = None, c.name
    return msg


def _draft_rm(db: Session, t: Terminal, csp: CSP, req: TerminalRequest, emailed_on: date, today: date) -> Optional[OutboundMessage]:
    from .comms.outbound import allowed_destinations
    rm = db.get(InternalUser, csp.rm_id) if csp.rm_id else None
    dests = allowed_destinations(db, csp, "RM", "EMAIL") if rm else []
    if not dests:
        req.blocked_reason = "No RM email address on the calling sheet for the follow-up"
        return None
    msg = OutboundMessage(csp_id=csp.id, template_name=RM_TEMPLATE, channel="EMAIL", destination=dests[0],
                          payload_json={**rm_email_text(t, csp, rm, req.lho or lho_name(t, csp), emailed_on, today),
                                        "terminal": t.id, "expiry_date": t.expiry_date.isoformat()},
                          idempotency_key=f"agent2:rm:{t.id}:{t.expiry_date.isoformat()}", stage="TERMINAL_EXTENSION",
                          recipient_role="RM", status=OutboundStatus.QUEUED_FOR_REVIEW, attempts=0, created_at=_now())
    msg = _add_message(db, msg)
    req.blocked_reason = None
    return msg


def _sent_local_day(m: OutboundMessage) -> Optional[date]:
    if m is None or m.status != OutboundStatus.SENT or not m.sent_at:
        return None
    from zoneinfo import ZoneInfo
    from .config import APP_TIMEZONE
    return m.sent_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(APP_TIMEZONE)).date()


def _request(db: Session, t: Terminal, csp: Optional[CSP]) -> TerminalRequest:
    req = (db.query(TerminalRequest)
           .filter(TerminalRequest.terminal_pk == t.id, TerminalRequest.expiry_date == t.expiry_date).first())
    if req is None:
        req = TerminalRequest(terminal_pk=t.id, csp_id=csp.id if csp else None, expiry_date=t.expiry_date,
                              lho=lho_name(t, csp) or None)
        db.add(req)
        db.flush()
    return req


def run(db: Session, today: Optional[date] = None) -> dict:
    """The daily job (worker, 09:15) and after each import: LHO emails due,
    RM follow-ups due, the 3-day alerts, expired requests closed."""
    s = settings(db)
    today = today or local_today()
    counts = {"lho_drafted": 0, "rm_drafted": 0, "alerts": 0, "not_ready": 0, "no_lho_email": 0, "expired": 0}
    for req in db.query(TerminalRequest).filter(TerminalRequest.outcome == "OPEN", TerminalRequest.expiry_date < today):
        req.outcome, req.closed_at = "EXPIRED", _now()
        counts["expired"] += 1
    db.commit()
    if not s["on"]:
        return {"skipped": "Agent 2 is switched off (Bank reports page)", **counts}
    from .comms import outbound
    horizon = today + timedelta(days=max(s["lead_days"], s["alert_days"]))
    terms = (db.query(Terminal).filter(Terminal.csp_id.isnot(None), Terminal.expiry_date.isnot(None),
                                       Terminal.expiry_date >= today, Terminal.expiry_date <= horizon)
             .order_by(Terminal.expiry_date).all())
    for t in terms:
        csp = db.get(CSP, t.csp_id)
        if csp is None or is_inactive(csp) or not csp.is_active_in_calling_sheet or (csp.state or "") == "TEST":
            continue
        days = (t.expiry_date - today).days
        req = (db.query(TerminalRequest)
               .filter(TerminalRequest.terminal_pk == t.id, TerminalRequest.expiry_date == t.expiry_date).first())
        if days <= s["lead_days"] and (req is None or req.lho_message_id is None):
            docs = _exact_docs(db, csp)
            ok, why = readiness(docs, t.expiry_date, s["required"])
            if ok:
                req = req or _request(db, t, csp)
                req.documents = {k: {"status": v["status"], "expiry": v["expiry"].isoformat() if v["expiry"] else None}
                                 for k, v in docs.items()}
                msg = _draft_lho(db, t, csp, req, docs, s)
                if msg is None:
                    counts["no_lho_email"] += 1
                else:
                    req.lho_message_id, req.lho_drafted_at = msg.id, _now()
                    counts["lho_drafted"] += 1
                    if not s["approval"] and msg.status == OutboundStatus.QUEUED_FOR_REVIEW:
                        outbound.approve(db, msg, "agent 2 (approval off)")
            else:
                counts["not_ready"] += 1
        if req is not None and req.lho_message_id and not req.rm_message_id and req.outcome == "OPEN":
            sent_on = _sent_local_day(db.get(OutboundMessage, req.lho_message_id))
            if sent_on and (today - sent_on).days >= s["rm_after_days"] and t.last_report_date \
                    and t.last_report_date >= sent_on:
                msg = _draft_rm(db, t, csp, req, sent_on, today)
                if msg is not None:
                    req.rm_message_id, req.rm_drafted_at = msg.id, _now()
                    counts["rm_drafted"] += 1
                    if not s["approval"] and msg.status == OutboundStatus.QUEUED_FOR_REVIEW:
                        outbound.approve(db, msg, "agent 2 (approval off)")
        if days <= s["alert_days"]:
            req = req or _request(db, t, csp)
            if req.alert_at is None and req.outcome == "OPEN":
                req.alert_at = _now()
                counts["alerts"] += 1
        db.commit()
    return counts


# ------------------------------------------------------------ the dashboard
def _bucket(days: Optional[int]) -> str:
    if days is None:
        return "nodate"
    if days < 0:
        return "expired"
    for limit, key in ((15, "d15"), (30, "d30"), (60, "d60"), (90, "d90")):
        if days <= limit:
            return key
    return "later"


def _msg_state(m: Optional[OutboundMessage]) -> Optional[dict]:
    if m is None:
        return None
    return {"id": m.id, "status": m.status.value, "to": m.destination,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None, "error": m.error_log}


def rows(db: Session, scope=None, today: Optional[date] = None, include_not_linked: bool = True) -> list[dict]:
    """Every terminal as one row for the Terminals page (and its Excel).
    `scope`: an extra SQL filter on CSP (an RM's own CSPs)."""
    s = settings(db)
    today = today or local_today()
    q = db.query(Terminal, CSP).outerjoin(CSP, CSP.id == Terminal.csp_id)
    if scope is not None:
        q = q.filter(Terminal.csp_id.isnot(None)).filter(scope)
    elif not include_not_linked:
        q = q.filter(Terminal.csp_id.isnot(None))
    pairs = q.all()
    staff = {u.id: u for u in db.query(InternalUser).all()}
    docs = document_dates(db, [c.id for _, c in pairs if c is not None], today)
    tids = [t.id for t, _ in pairs] or [-1]
    reqs = {(r.terminal_pk, r.expiry_date): r for r in
            db.query(TerminalRequest).filter(TerminalRequest.terminal_pk.in_(tids))}
    msg_ids = {i for r in reqs.values() for i in (r.lho_message_id, r.rm_message_id) if i} or {-1}
    msgs = {m.id: m for m in db.query(OutboundMessage).filter(OutboundMessage.id.in_(msg_ids))}
    renewals = {}
    for h in (db.query(TerminalExpiry).filter(TerminalExpiry.terminal_pk.in_(tids),
                                               TerminalExpiry.previous_expiry_date.isnot(None))
              .order_by(TerminalExpiry.seen_at)):
        if h.expiry_date > h.previous_expiry_date:
            renewals[h.terminal_pk] = h
    out = []
    for t, c in pairs:
        days = (t.expiry_date - today).days if t.expiry_date else None
        req = reqs.get((t.id, t.expiry_date))
        lho_m = msgs.get(req.lho_message_id) if req and req.lho_message_id else None
        rm_m = msgs.get(req.rm_message_id) if req and req.rm_message_id else None
        if c is not None:
            ok, why = readiness(docs.get(c.id, {}), t.expiry_date, s["required"])
            if is_inactive(c):
                ok, why = False, "Terminal Inactive on the calling sheet: no email is sent"
        else:
            ok, why = False, {"TWO_CSPS": "Two CSPs have this KO code"}.get(t.link_state, "No CSP has this KO code")
        if c is None:
            status = "not_linked"
        elif days is not None and days < 0:
            status = "expired"
        elif rm_m is not None and rm_m.status == OutboundStatus.QUEUED_FOR_REVIEW:
            status = "approval"
        elif rm_m is not None:
            status = "rm"
        elif lho_m is not None and lho_m.status == OutboundStatus.SENT:
            status = "emailed"
        elif lho_m is not None and lho_m.status in (OutboundStatus.QUEUED_FOR_REVIEW, OutboundStatus.APPROVED):
            status = "approval" if lho_m.status == OutboundStatus.QUEUED_FOR_REVIEW else "emailed"
        elif lho_m is not None and lho_m.status == OutboundStatus.REJECTED:
            status = "rejected"
        elif lho_m is not None:
            status = "failed"
        elif req is not None and req.blocked_reason and ok:
            status = "no_lho"
        elif not ok:
            status = "not_ready"
        elif days is not None and days <= s["lead_days"]:
            status = "due"
        else:
            status = "ready"
        ren = renewals.get(t.id)
        rm, dc = (staff.get(c.rm_id), staff.get(c.dc_id)) if c is not None else (None, None)
        out.append({
            "id": t.id, "terminal_id": t.terminal_id, "ko_code": t.ko_code, "link_state": t.link_state,
            "csp": {"id": c.id, "name": c.name, "code": c.current_code} if c is not None else None,
            "name_in_report": t.name_in_report,
            "rm": rm.name if rm else ("No RM" if c is not None else "Not linked"), "rm_id": c.rm_id if c is not None else None,
            "dc": dc.name if dc else ("No DC" if c is not None else "Not linked"),
            "lho": lho_name(t, c) or "LHO not given", "branch": t.branch or (c.branch if c is not None else None),
            "expiry_date": t.expiry_date.isoformat() if t.expiry_date else None, "days_left": days,
            "bucket": _bucket(days), "status": status, "status_label": STATUSES[status],
            "ready": ok, "reason": why,
            "docs": {k: {"status": v["status"], "expiry": v["expiry"].isoformat() if v["expiry"] else None}
                     for k, v in (docs.get(c.id, {}) if c is not None else {}).items()},
            "lho_email": _msg_state(lho_m), "rm_followup": _msg_state(rm_m),
            "blocked_reason": req.blocked_reason if req else None,
            "alert": bool(req and req.alert_at and req.outcome == "OPEN"),
            "renewed": ({"on": ren.report_date.isoformat() if ren.report_date else None,
                         "from": ren.previous_expiry_date.isoformat(), "to": ren.expiry_date.isoformat(),
                         "months": round((ren.expiry_date - ren.previous_expiry_date).days / 30.44)}
                        if ren and ren.expiry_date == t.expiry_date and ren.report_date
                        and (today - ren.report_date).days <= RENEWED_RECENT_DAYS else None),
            "last_report_date": t.last_report_date.isoformat() if t.last_report_date else None,
        })
    out.sort(key=lambda r: (r["days_left"] is None, r["days_left"] if r["days_left"] is not None else 0))
    return out


def for_csp(db: Session, csp_id: int) -> list[dict]:
    """The CSP page's Terminal block: each terminal, its expiry history and requests."""
    c = db.get(CSP, csp_id)
    out = []
    for r in [r for r in rows(db, CSP.id == csp_id)]:
        hist = (db.query(TerminalExpiry).filter(TerminalExpiry.terminal_pk == r["id"])
                .order_by(TerminalExpiry.seen_at.desc()).limit(12).all())
        r["history"] = [{"expiry_date": h.expiry_date.isoformat(), "from": h.previous_expiry_date.isoformat()
                         if h.previous_expiry_date else None, "report_date": h.report_date.isoformat() if h.report_date else None}
                        for h in hist]
        out.append(r)
    return out if c is not None else []


def last_import(db: Session) -> Optional[dict]:
    i = db.query(TerminalImport).order_by(TerminalImport.report_date.desc(), TerminalImport.id.desc()).first()
    return None if i is None else {"id": i.id, "report_date": i.report_date.isoformat(), "file_name": i.file_name,
                                   "uploaded_by": i.uploaded_by,
                                   "uploaded_at": i.uploaded_at.isoformat() if i.uploaded_at else None}
