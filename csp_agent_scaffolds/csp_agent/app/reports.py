"""
app/reports.py
The Excel reports, built from the same data the dashboard shows:

  CSP_Report_<date>.xlsx       Summary, All CSPs, one tab per category,
                               Expiring in 60 days
  Contacts_<date>.xlsx         All CSP contacts (CSP, RM, DC phone/email),
                               RM & DC directory
  Contact_Gaps_<date>.xlsx     Summary, CSP contact missing, RM gaps, DC gaps,
                               RM & DC missing details, Contact changes from CSPs

The worker writes all three every morning into <vault parent>/reports/ (the last
REPORTS_KEEP_DAYS days are kept); the dashboard can also download them live.
"""
import io
from types import SimpleNamespace
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from . import vault
from .compliance import DOC_LABELS, REQUIRED_TYPES, SLAB_NAMES, evaluate, slab_label, sub_slab, sub_slab_label
from .models import CSP, ContactChangeRequest, InboundMessage, InternalUser, OutboundMessage, OutboundStatus

logger = logging.getLogger(__name__)

REPORTS_KEEP_DAYS = 30
EXPIRING_WITHIN_DAYS = 60
HEADER = PatternFill("solid", fgColor="0092CC")        # kiosk.eko.in primary
RED = PatternFill("solid", fgColor="FDE2E2")
AMBER = PatternFill("solid", fgColor="FDF1D6")
GREEN = PatternFill("solid", fgColor="E3F4EA")
SHORT = {"AGREEMENT": "Agreement", "POLICE_VERIFICATION": "PVR", "IIBF_CERTIFICATE": "IIBF"}
GAP_TEXT = {
    "phone_missing": "Phone missing", "email_missing": "Email missing", "not_assigned": "Not assigned",
    "rm_contact_missing": "RM phone/email missing", "dc_contact_missing": "DC phone/email missing",
}


def _gap_text(codes) -> str:
    return ", ".join(GAP_TEXT.get(c, c.replace("_", " ").capitalize()) for c in (codes or []))


def _sheet(wb: Workbook, title: str, head: list[str], rows: list[list], first: bool = False,
           fills: Optional[list[dict[int, PatternFill]]] = None):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    ws.append(head)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), HEADER
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for i, row in enumerate(rows):
        ws.append([vault._cell(v) for v in row])
        for col, fill in (fills[i] if fills else {}).items():
            ws.cell(row=ws.max_row, column=col).fill = fill
    for i, h in enumerate(head, 1):
        longest = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:300] if i - 1 < len(r) and r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = max(10, min(50, longest + 2))
    ws.freeze_panes = "C2" if len(head) > 3 else "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _state_fill(status: str, days_left: Optional[int]) -> Optional[PatternFill]:
    if status in ("EXPIRED", "MISSING", "UNREADABLE"):
        return RED
    if days_left is not None and days_left <= EXPIRING_WITHIN_DAYS:
        return AMBER
    return GREEN if status == "VALID" else None


def _save(wb: Workbook) -> io.BytesIO:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ------------------------------------------------------------ CSP report
def csp_report(db: Session, today: Optional[date] = None, csp_ids: Optional[list[int]] = None) -> io.BytesIO:
    """The CSP report; csp_ids limits it to those CSPs (e.g. one DC's)."""
    today = today or date.today()
    staff = {u.id: u for u in db.query(InternalUser).all()}
    q = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True))
    if csp_ids is not None:
        q = q.filter(CSP.id.in_(csp_ids or [-1]))
    csps = q.order_by(CSP.current_code).all()

    head = ["CSP code", "CSP name", "State", "Branch", "Phone", "Email", "RM", "DC", "Label", "Label name",
            "Sub-group", "Reason"]
    for t in REQUIRED_TYPES:
        head += [f"{SHORT[t]} status", f"{SHORT[t]} issued", f"{SHORT[t]} expires", f"{SHORT[t]} days left"]
    head += ["Next action", "Folder"]

    rows, fills, by_cat, expiring, renewal = [], [], {1: [], 2: [], 3: [], 4: []}, [], []
    missing = {t: 0 for t in REQUIRED_TYPES}
    for c in csps:
        st = evaluate(db, c, today)
        rm, dc = staff.get(c.rm_id), staff.get(c.dc_id)
        row = [c.current_code, c.name, c.state, c.branch, c.phone, c.email, rm.name if rm else None,
               dc.name if dc else None, st.category, SLAB_NAMES[st.category], sub_slab_label(sub_slab(st)), st.reason]
        fill = {}
        for t in REQUIRED_TYPES:
            s = st.docs[t]
            expires = s.expiry_date or ("lifetime" if t == "IIBF_CERTIFICATE" and s.issue_date else None)
            f = _state_fill(s.status, s.days_left)
            if f is not None:
                fill[len(row) + 1] = f
            row += [s.status, s.issue_date, expires, s.days_left]
            if s.status in ("MISSING", "UNREADABLE"):
                missing[t] += 1
            if s.status == "VALID" and s.days_left is not None and s.days_left <= EXPIRING_WITHIN_DAYS:
                expiring.append([c.current_code, c.name, rm.name if rm else None, dc.name if dc else None,
                                 DOC_LABELS[t][0], s.issue_date, s.expiry_date, s.days_left])
        row += [c.next_action_at, vault.folder_name(c.current_code, c.name)]   # a date column
        rows.append(row)
        fills.append(fill)
        by_cat[st.category].append((row, fill))
        if st.expired:
            renewal.append((row, fill))

    queued = db.query(OutboundMessage).filter(OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW).count()
    summary = [["Generated", datetime.now().strftime("%d-%m-%Y %H:%M")], ["Active CSPs (calling sheet)", len(csps)]]
    summary += [[slab_label(k), len(v)] for k, v in by_cat.items()]
    summary += [["CSPs with an expired document (any label)", len(renewal)]]
    summary += [[f"{DOC_LABELS[t][0]} missing or unreadable", missing[t]] for t in REQUIRED_TYPES]
    summary += [[f"Documents expiring within {EXPIRING_WITHIN_DAYS} days", len(expiring)],
                ["Messages waiting for approval", queued]]

    wb = Workbook()
    _sheet(wb, "Summary", ["Item", "Count"], summary, first=True)
    _sheet(wb, "All CSPs", head, rows, fills=fills)
    for k, items in by_cat.items():
        _sheet(wb, f"Label {k} {SLAB_NAMES[k]}", head, [r for r, _ in items], fills=[f for _, f in items])
    _sheet(wb, "Renewal due", head, [r for r, _ in renewal], fills=[f for _, f in renewal])
    expiring.sort(key=lambda r: r[-1])
    _sheet(wb, f"Expiring {EXPIRING_WITHIN_DAYS} days",
           ["CSP code", "CSP name", "RM", "DC", "Document", "Issued", "Expires", "Days left"], expiring,
           fills=[{8: AMBER} for _ in expiring])
    return _save(wb)


# ------------------------------------------------------- contacts & gaps
# ------------------------------------------------------------- DC report
def _dc_doc_text(t: str, s, today: date) -> tuple[str, Optional[PatternFill]]:
    """One document in plain words for a DC, with its colour."""
    fmt = lambda d: d.strftime("%d-%m-%Y")
    if s.in_review:
        return "Being checked by Eko – nothing to do", AMBER
    if s.status == "VALID":
        if s.expiry_date is None:
            return "OK (no expiry)", GREEN
        if s.days_left is not None and s.days_left <= EXPIRING_WITHIN_DAYS:
            return f"Renew now – expires {fmt(s.expiry_date)} ({s.days_left} days left)", AMBER
        return f"OK till {fmt(s.expiry_date)}", GREEN
    applied = f" (applied for a new one on {fmt(s.applied_on)}, waiting)" if s.applied_on else ""
    if s.status == "EXPIRED":
        return (f"EXPIRED on {fmt(s.expiry_date)} – upload the renewed one" if s.expiry_date
                else "EXPIRED – upload the renewed one") + applied, RED
    if s.status == "UNREADABLE":
        return "Not readable – upload a clear scan", RED
    return "MISSING – upload it" + applied, RED


# The CSPs with no DC, reported like a DC (dashboard: "No DC"; live sheet name "No DC").
NO_DC = SimpleNamespace(id=None, name="No DC")


def _dc_rows(db: Session, dc: InternalUser, today: date) -> list[tuple]:
    """(documents to upload, CSP, RM, [(text, fill)] per document, [labels to
    upload], ComplianceState) for each of the DC's working CSPs, needs first."""
    from .compliance import working_clause
    staff = {u.id: u for u in db.query(InternalUser).all()}
    rows = []
    which = CSP.dc_id.is_(None) if dc.id is None else CSP.dc_id == dc.id      # NO_DC: CSPs with no DC
    for c in db.query(CSP).filter(working_clause(), which).order_by(CSP.current_code):
        st = evaluate(db, c, today)
        cells, todo = [], []
        for t in REQUIRED_TYPES:
            s = st.docs[t]
            text, fill = _dc_doc_text(t, s, today)
            cells.append((text, fill))
            if fill is RED or (fill is AMBER and not s.in_review):
                todo.append(DOC_LABELS[t][0])
        rows.append((len(todo), c, staff.get(c.rm_id), cells, todo, st))
    rows.sort(key=lambda r: (-r[0], r[1].current_code))
    return rows


DC_SHORT = {"AGREEMENT": "Agreement", "POLICE_VERIFICATION": "Police Verification", "IIBF_CERTIFICATE": "IIBF"}
DC_SHORT_HI = {"AGREEMENT": "एग्रीमेंट", "POLICE_VERIFICATION": "पुलिस वेरिफिकेशन", "IIBF_CERTIFICATE": "IIBF सर्टिफिकेट"}


def dc_summary_text(db: Session, dc: InternalUser, today: Optional[date] = None) -> str:
    """One DC's CSPs as a short text (to paste into WhatsApp): numbers first,
    then which CSP needs which document. Hindi and English."""
    today = today or date.today()
    rows = _dc_rows(db, dc, today)
    fmt = lambda d: d.strftime("%d-%m-%Y")
    total, need = len(rows), [r for r in rows if r[0]]
    by_n = {n: sum(1 for r in rows if r[0] == n) for n in (1, 2, 3)}
    out = [f"*CSP डॉक्यूमेंट रिपोर्ट / CSP document report*", f"DC: {dc.name}", f"तारीख / Date: {fmt(today)}", "",
           f"कुल CSP / Total CSPs: *{total}*",
           f"✅ सारे 3 डॉक्यूमेंट ठीक / All 3 documents OK: *{total - len(need)}*",
           f"❗ डॉक्यूमेंट चाहिए / Need documents: *{len(need)}*"]
    out += [f"   • {n} डॉक्यूमेंट / {n} document{'s' if n > 1 else ''}: {by_n[n]}" for n in (3, 2, 1) if by_n[n]]
    out += ["", "*कौन सा डॉक्यूमेंट कितने CSP को चाहिए / Documents to upload:*"]
    soon, checking = [], 0
    for t in REQUIRED_TYPES:
        miss = exp = bad = 0
        for r in rows:
            s = r[5].docs[t]
            if s.in_review:
                checking += 1
            elif s.status == "VALID" and s.days_left is not None and s.days_left <= EXPIRING_WITHIN_DAYS:
                soon.append((s.expiry_date, r[1], t))
            elif s.status == "EXPIRED":
                exp += 1
            elif s.status == "UNREADABLE":
                bad += 1
            elif s.status != "VALID":
                miss += 1
        parts = [f"नहीं है / missing {miss}"] + ([f"खत्म / expired {exp}"] if exp else []) \
            + ([f"साफ़ नहीं / not readable {bad}"] if bad else [])
        out.append(f"• {DC_SHORT_HI[t]} / {DC_SHORT[t]}: *{miss + exp + bad}* ({', '.join(parts)})")
    out.append(f"• 60 दिन में रिन्यू / Renew within 60 days: *{len(soon)}*")
    out.append(f"• Eko जाँच कर रहा है / Being checked by Eko: *{checking}*")
    applied = sum(1 for r in rows if r[5].docs["POLICE_VERIFICATION"].applied_on)
    if applied:
        out.append(f"• पुलिस वेरिफिकेशन का आवेदन किया, इंतज़ार / PVR applied, waiting: *{applied}*")
    if need:
        out += ["", "*CSP सूची (ज़्यादा ज़रूरी पहले) / CSP list (most needed first):*"]
        for i, r in enumerate(need, 1):
            c = r[1]
            names = [DC_SHORT[t] for t in REQUIRED_TYPES if DOC_LABELS[t][0] in r[4]]
            out.append(f"{i}. {c.current_code} {c.name} – {r[0]} डॉक्यूमेंट / doc{'s' if r[0] > 1 else ''}: {', '.join(names)}")
    if soon:
        out += ["", "*जल्दी रिन्यू करें / Renew soon:*"]
        for exp_date, c, t in sorted(soon, key=lambda x: x[0]):
            out.append(f"• {c.current_code} {c.name}: {DC_SHORT[t]} – {fmt(exp_date)} ({(exp_date - today).days} दिन / days)")
    out += ["", "अपलोड के लिए DC लिंक में KO कोड डालें। / Enter the KO code in the DC link to upload."]
    return "\n".join(out)


def dc_progress(db: Session, today: Optional[date] = None) -> dict:
    """Per DC: their working CSPs' documents on the previous day (the latest
    daily Label snapshot before today, app/monitoring.py) and now."""
    from sqlalchemy import func
    from .compliance import working_clause
    from .models import LabelSnapshot
    today = today or date.today()
    base = db.query(func.max(LabelSnapshot.day)).filter(LabelSnapshot.day < today).scalar()
    old = {cid: docs or {} for cid, docs in
           db.query(LabelSnapshot.csp_id, LabelSnapshot.docs).filter(LabelSnapshot.day == base)} if base else {}
    names = {u.id: u.name for u in db.query(InternalUser)}
    keys = ("csps", "left_now", "complete_before", "complete_now", "valid_before", "valid_now", "improved", "worse", "need_now")
    by_dc: dict = {}
    for c in db.query(CSP).filter(working_clause()):
        st = evaluate(db, c, today)
        now_valid = sum(1 for s in st.docs.values() if s.status == "VALID" and not s.in_review)
        before = old.get(c.id)
        was_valid = sum(1 for v in before.values() if v == "VALID") if before is not None else now_valid
        r = by_dc.setdefault(names.get(c.dc_id) or "No DC", dict.fromkeys(keys, 0))
        r["csps"] += 1
        r["left_now"] += st.category in (2, 3, 4)          # documents still to upload (Labels 2-4)
        r["valid_before"] += was_valid
        r["valid_now"] += now_valid
        r["complete_before"] += was_valid == 3
        r["complete_now"] += now_valid == 3
        r["improved"] += now_valid > was_valid
        r["worse"] += now_valid < was_valid
        r["need_now"] += now_valid < 3
    rows = [{"dc": k, **v} for k, v in sorted(by_dc.items(), key=lambda kv: (kv[0] == "No DC", kv[0].lower()))]
    total = {k: sum(r[k] for r in rows) for k in keys}
    return {"base_day": base.isoformat() if base else None, "today": today.isoformat(), "rows": rows, "total": total}


def dc_table(db: Session, dc: InternalUser, today: Optional[date] = None) -> dict:
    """The DC Excel's content as plain data, for the DCs' live Google Sheets
    (app/dc_sheets.py): title lines, header, one row per CSP and each cell's
    colour ("green" / "yellow" / "red" / None)."""
    today = today or date.today()
    rows = _dc_rows(db, dc, today)
    need = sum(1 for r in rows if r[0])
    colour = {id(GREEN): "green", id(AMBER): "yellow", id(RED): "red"}
    out, fills = [], []
    for n, c, rm, cells, todo, _ in rows:
        out.append([c.current_code, c.name, c.phone or "", c.state or "",
                    ", ".join(todo) if todo else "Nothing – all OK"] + [t for t, _ in cells] + [rm.name if rm else ""])
        fills.append([None, None, None, None, "red" if todo else "green"]
                     + [colour.get(id(f)) if f is not None else None for _, f in cells] + [None])
    return {"title": f"CSP documents – DC {dc.name}",
            "summary": f"{len(rows)} CSPs · {need} need a document · {len(rows) - need} have everything",
            "legend": "Colours: green = OK · yellow = renew soon / being checked · red = upload needed. "
                      "CSPs that need something are at the top.",
            "header": ["KO code", "CSP name", "Phone", "State", "What to upload", "Agreement", "Police Verification",
                       "IIBF Certificate", "RM"],
            "rows": out, "fills": fills}


def dc_report(db: Session, dc: InternalUser, today: Optional[date] = None) -> io.BytesIO:
    """One DC's working CSPs (Labels 1-4), one row each, in plain words: what
    is on file and what to upload. CSPs with something to do come first."""
    today = today or date.today()
    rows = _dc_rows(db, dc, today)

    wb = Workbook()
    ws = wb.active
    ws.title = "My CSPs"
    need = sum(1 for r in rows if r[0])
    ws.append([f"CSP documents – DC {dc.name}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"As on {today.strftime('%d-%m-%Y')} · {len(rows)} CSPs · {need} need a document · "
               f"{len(rows) - need} have everything"])
    ws.append(["Colours:  green = OK  ·  yellow = renew soon / being checked  ·  red = upload needed. "
               "CSPs that need something are at the top."])
    ws.append([])
    head = ["KO code", "CSP name", "Phone", "State", "What to upload", "Agreement", "Police Verification",
            "IIBF Certificate", "RM"]
    ws.append(head)
    hr = ws.max_row
    for cell in ws[hr]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), HEADER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for n, c, rm, cells, todo, _ in rows:
        ws.append([c.current_code, c.name, c.phone, c.state, ", ".join(todo) if todo else "Nothing – all OK"]
                  + [t for t, _ in cells] + [rm.name if rm else ""])
        r = ws.max_row
        ws.cell(row=r, column=5).fill = RED if todo else GREEN
        ws.cell(row=r, column=5).font = Font(bold=True)
        for i, (_, fill) in enumerate(cells):
            if fill is not None:
                ws.cell(row=r, column=6 + i).fill = fill
    for col, w in zip("ABCDEFGHI", (11, 26, 13, 14, 34, 34, 34, 34, 14)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=hr + 1):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    ws.freeze_panes = ws.cell(row=hr + 1, column=3)
    if rows:
        ws.auto_filter.ref = f"A{hr}:I{ws.max_row}"
    return _save(wb)


def _staff_rows(db: Session, csps: list[CSP], staff: dict) -> list[list]:
    """RM / DC directory: name, role, number of CSPs, phone, email, what is missing."""
    counts: dict[int, int] = {}
    for c in csps:
        for uid in (c.rm_id, c.dc_id):
            if uid:
                counts[uid] = counts.get(uid, 0) + 1
    people = sorted((u for u in staff.values() if u.role in ("RM", "DC")), key=lambda u: (u.role, u.name or ""))
    return [[u.name, u.role, counts.get(u.id, 0), u.phone, u.email,
             ", ".join(x for x, v in (("Phone missing", u.phone), ("Email missing", u.email)) if not v)]
            for u in people]


def contacts_report(db: Session) -> io.BytesIO:
    """Contacts: every active CSP with its own, its RM's and its DC's phone
    and email, plus the RM & DC directory. Empty cells are red."""
    staff = {u.id: u for u in db.query(InternalUser).all()}
    csps = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.current_code).all()
    head = ["CSP code", "CSP name", "Phone", "Alt phone", "WhatsApp", "Email",
            "RM", "RM phone", "RM email", "DC", "DC phone", "DC email", "State", "Branch"]
    rows, fills = [], []
    for c in csps:
        rm, dc = staff.get(c.rm_id), staff.get(c.dc_id)
        row = [c.current_code, c.name, c.phone, c.alt_phone, c.whatsapp_number, c.email,
               rm.name if rm else None, rm.phone if rm else None, rm.email if rm else None,
               dc.name if dc else None, dc.phone if dc else None, dc.email if dc else None, c.state, c.branch]
        rows.append(row)
        # Highlight the contact fields that are empty (alt phone and WhatsApp are optional).
        fills.append({i + 1: RED for i in (2, 5, 6, 7, 8, 9, 10, 11) if not row[i]})
    directory = _staff_rows(db, csps, staff)
    wb = Workbook()
    _sheet(wb, "All CSP contacts", head, rows, first=True, fills=fills)
    _sheet(wb, "RM & DC directory", ["Name", "Role", "CSPs", "Phone", "Email", "Missing"], directory,
           fills=[{6: RED} if r[-1] else {} for r in directory])
    return _save(wb)


def gaps_report(db: Session) -> io.BytesIO:
    """Gaps only: what is missing, CSP by CSP, and for RMs / DCs."""
    staff = {u.id: u for u in db.query(InternalUser).all()}
    csps = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.current_code).all()
    csp_rows, rm_rows, dc_rows = [], [], []
    for c in csps:
        g = c.contact_gaps or {}
        rm, dc = staff.get(c.rm_id), staff.get(c.dc_id)
        if g.get("csp"):
            csp_rows.append([c.current_code, c.name, c.phone, c.alt_phone, c.email, _gap_text(g["csp"]),
                             rm.name if rm else None, dc.name if dc else None])
        if g.get("rm"):
            rm_rows.append([c.current_code, c.name, rm.name if rm else None, rm.phone if rm else None,
                            rm.email if rm else None, _gap_text(g["rm"])])
        if g.get("dc"):
            dc_rows.append([c.current_code, c.name, dc.name if dc else None, dc.phone if dc else None,
                            dc.email if dc else None, _gap_text(g["dc"])])
    staff_gaps = [r for r in _staff_rows(db, csps, staff) if r[-1]]
    by_id = {c.id: c for c in csps}
    changes = [[by_id[r.csp_id].current_code if r.csp_id in by_id else r.csp_id,
                by_id[r.csp_id].name if r.csp_id in by_id else None, r.field, r.old_value, r.new_value,
                r.created_at.strftime("%d-%m-%Y %H:%M") if r.created_at else None]
               for r in db.query(ContactChangeRequest).filter_by(status="PENDING").order_by(ContactChangeRequest.id)]

    summary = [["Generated", datetime.now().strftime("%d-%m-%Y %H:%M")], ["Active CSPs (calling sheet)", len(csps)],
               ["CSPs with phone or email missing", len(csp_rows)],
               ["CSPs with no RM, or RM contact missing", len(rm_rows)],
               ["CSPs with no DC, or DC contact missing", len(dc_rows)],
               ["RMs/DCs with phone or email missing", len(staff_gaps)],
               ["Contact changes sent by CSPs, not yet checked", len(changes)]]

    wb = Workbook()
    _sheet(wb, "Summary", ["Item", "Count"], summary, first=True)
    _sheet(wb, "CSP contact missing", ["CSP code", "CSP name", "Phone", "Alt phone", "Email", "Missing", "RM", "DC"],
           csp_rows, fills=[{6: RED} for _ in csp_rows])
    _sheet(wb, "RM gaps", ["CSP code", "CSP name", "RM", "RM phone", "RM email", "Missing"], rm_rows,
           fills=[{6: RED} for _ in rm_rows])
    _sheet(wb, "DC gaps", ["CSP code", "CSP name", "DC", "DC phone", "DC email", "Missing"], dc_rows,
           fills=[{6: RED} for _ in dc_rows])
    _sheet(wb, "RM & DC missing details", ["Name", "Role", "CSPs", "Phone", "Email", "Missing"], staff_gaps,
           fills=[{6: RED} for _ in staff_gaps])
    _sheet(wb, "Contact changes from CSPs", ["CSP code", "CSP name", "Field", "Old value", "New value", "Sent on"],
           changes)
    return _save(wb)


# ------------------------------------------------------------- nightly
def reports_dir() -> Path:
    return vault.ROOT.parent / "reports"


def write_daily(db: Session, today: Optional[date] = None) -> dict:
    """Write today's reports (CSP report, contacts, gaps) and drop ones older than REPORTS_KEEP_DAYS."""
    today = today or date.today()
    d = reports_dir()
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for prefix, buf in (("CSP_Report", csp_report(db, today)), ("Contacts", contacts_report(db)),
                        ("Contact_Gaps", gaps_report(db))):
        final = d / f"{prefix}_{today.isoformat()}.xlsx"
        tmp = final.with_suffix(".xlsx.part")
        tmp.write_bytes(buf.getvalue())
        tmp.replace(final)
        written.append(final.name)
    cutoff = today - timedelta(days=REPORTS_KEEP_DAYS)
    removed = 0
    for f in d.glob("*_????-??-??.xlsx"):
        try:
            if date.fromisoformat(f.stem[-10:]) < cutoff:
                f.unlink()
                removed += 1
        except ValueError:
            continue
    return {"written": written, "folder": str(d), "removed_old": removed}


# --------------------------------------------------- unmatched emails
_CODE = re.compile(r"\b\d[A-Z]\d{6}\b")
_REPORT_SUBJECT = re.compile(r"pendency\s+report|report\s+as\s+on|\bmis\b|summary\s+report", re.I)


def unmatched_emails(db: Session) -> dict:
    """Emails that no CSP on the calling sheet matched, sorted into: a CSP
    code that is not on the sheet, a bulk report, or no CSP code at all."""
    rows = db.query(InboundMessage).filter(InboundMessage.status == "UNMATCHED_NO_CSP") \
        .order_by(InboundMessage.received_at.desc().nullslast()).all()
    by_code: dict[str, dict] = {}
    items = []
    for m in rows:
        note = m.error_message or ""
        seen = re.search(r"code seen: ([^,)]+(?:,\s*\d[A-Z]\d{6})*)", note)
        name = re.search(r"name seen: ([^)]+)\)", note)
        codes = list(dict.fromkeys(_CODE.findall(((seen.group(1) if seen else "") + " " + (m.subject or "")).upper())))
        kind = "code_not_on_sheet" if codes else ("report" if _REPORT_SUBJECT.search(m.subject or "") else "no_code")
        item = {"id": m.id, "received_at": m.received_at.isoformat() if m.received_at else None,
                "subject": m.subject, "sender": m.sender, "codes": codes, "kind": kind,
                "name_seen": (name.group(1).strip() if name and name.group(1).strip() != "none" else None)}
        items.append(item)
        for c in codes:
            e = by_code.setdefault(c, {"code": c, "emails": 0, "latest": None, "names": set(), "subjects": []})
            e["emails"] += 1
            e["latest"] = e["latest"] or item["received_at"]
            if item["name_seen"]:
                e["names"].add(item["name_seen"])
            if len(e["subjects"]) < 3:
                e["subjects"].append(m.subject)
    codes = sorted(by_code.values(), key=lambda e: (-e["emails"], e["code"]))
    for e in codes:
        e["names"] = sorted(e["names"])
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in ("code_not_on_sheet", "report", "no_code")}
    return {"total": len(items), "counts": counts, "codes": codes, "emails": items}


def unmatched_report(db: Session) -> io.BytesIO:
    u = unmatched_emails(db)
    kind_text = {"code_not_on_sheet": "CSP code not on calling sheet", "report": "Bulk report (not one CSP)",
                 "no_code": "No CSP code in the email"}
    wb = Workbook()
    _sheet(wb, "Summary", ["Item", "Count"], [["Emails not matched to a CSP", u["total"]]]
           + [[kind_text[k], v] for k, v in u["counts"].items()] + [["Different CSP codes not on sheet", len(u["codes"])]],
           first=True)
    _sheet(wb, "Codes not on calling sheet", ["CSP code", "Emails", "Latest email", "Name in email", "Subjects"],
           [[e["code"], e["emails"], (e["latest"] or "")[:10], ", ".join(e["names"]), " | ".join(s or "" for s in e["subjects"])]
            for e in u["codes"]])
    _sheet(wb, "All unmatched emails", ["Received", "Type", "CSP codes", "Name in email", "Subject", "Sender"],
           [[(i["received_at"] or "")[:10], kind_text[i["kind"]], ", ".join(i["codes"]), i["name_seen"], i["subject"],
             i["sender"]] for i in u["emails"]])
    return _save(wb)
