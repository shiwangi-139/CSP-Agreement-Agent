"""
app/api/terminals.py
Agent 2 on the dashboard (app/terminals.py): the Terminals page (every
signed-in user; an RM sees only their own CSPs' terminals) and the Bank
reports page (admins: upload the terminal report, LHO contacts, links,
Agent 2's switch). Mounted under /api/hub.
"""
import io
from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import terminals as T
from ..auth import Principal, require_admin, require_user
from ..db import get_db
from ..models import CSP, LhoContact, OutboundMessage, OutboundStatus, Terminal, TerminalImport, TerminalRequest
from .hub import NOT_TEST, _rm_filter

router = APIRouter(dependencies=[Depends(require_user)])
MAX_REPORT_BYTES = 15 * 1024 * 1024


def _scope(me: Principal):
    return None if me.is_admin else (NOT_TEST & _rm_filter(me))


def _filtered(rows: list[dict], bucket: str, status: str, rm: str, dc: str, lho: str, q: str) -> list[dict]:
    if bucket:
        rows = [r for r in rows if r["bucket"] == bucket]
    if status:
        rows = ([r for r in rows if r["alert"]] if status == "alert" else [r for r in rows if r["status"] == status])
    if rm:
        rows = [r for r in rows if r["rm"] == rm]
    if dc:
        rows = [r for r in rows if r["dc"] == dc]
    if lho:
        rows = [r for r in rows if r["lho"] == lho]
    if q.strip():
        n = q.strip().lower()
        rows = [r for r in rows if n in (r["ko_code"] or "").lower() or n in (r["terminal_id"] or "").lower()
                or (r["csp"] and n in r["csp"]["name"].lower()) or n in (r["name_in_report"] or "").lower()]
    return rows


def _groups(rows: list[dict], key: str) -> list[dict]:
    """One line per RM / DC / LHO: terminals in each days-left group, and how
    many are not ready or need a person."""
    out: dict[str, dict] = {}
    for r in rows:
        g = out.setdefault(r[key], {"name": r[key], "total": 0, "not_ready": 0, "alerts": 0,
                                    **{b: 0 for b, _ in T.BUCKETS}})
        g["total"] += 1
        g[r["bucket"]] += 1
        g["not_ready"] += r["status"] == "not_ready"
        g["alerts"] += r["alert"] or r["status"] == "expired"
    return sorted(out.values(), key=lambda g: (-(g["expired"] + g["d15"]), -g["total"], g["name"].lower()))


@router.get("/terminals")
def terminals_page(bucket: str = "", status: str = "", rm: str = "", dc: str = "", lho: str = "", q: str = "",
                   group: str = "", me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    all_rows = T.rows(db, _scope(me))
    # Counts on the tiles and filter lists come from the rows the other filters leave.
    in_scope = _filtered(all_rows, "", "", rm, dc, lho, q)
    shown = _filtered(in_scope, bucket, status, "", "", "", "")
    opts = lambda key: [{"name": k, "n": n} for k, n in sorted(Counter(r[key] for r in all_rows).items(),
                                                                key=lambda x: x[0].lower())]
    s = T.settings(db)
    return {
        "settings": {k: s[k] for k in ("on", "approval", "lead_days", "rm_after_days", "alert_days", "required")},
        "last_import": T.last_import(db), "total": len(all_rows),
        "buckets": [{"key": k, "label": label, "n": sum(r["bucket"] == k for r in in_scope)} for k, label in T.BUCKETS],
        "statuses": [{"key": k, "label": label, "n": sum(r["status"] == k for r in in_scope)}
                     for k, label in T.STATUSES.items() if me.is_admin or k != "not_linked"],
        "alerts": sum(r["alert"] for r in in_scope),
        "options": {"rm": opts("rm"), "dc": opts("dc"), "lho": opts("lho")},
        "groups": _groups(shown, group) if group in ("rm", "dc", "lho") else None,
        "rows": shown[:2000], "shown": len(shown),
    }


@router.get("/terminals/summary")
def terminals_summary(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """For Today: what Agent 2 needs from a person."""
    if not db.query(Terminal.id).first():
        return {"any": False}
    rows = T.rows(db, _scope(me), include_not_linked=False)
    s = T.settings(db)
    return {"any": True, "on": s["on"], "alert_days": s["alert_days"], "last_import": T.last_import(db),
            "approval": sum(r["status"] == "approval" for r in rows),
            "rm": sum(r["status"] == "rm" for r in rows),
            "alerts": sum(r["alert"] for r in rows),
            "expired": sum(r["status"] == "expired" for r in rows),
            "not_ready_15": sum(r["status"] == "not_ready" and r["bucket"] == "d15" for r in rows),
            "no_lho": sum(r["status"] == "no_lho" for r in rows)}


@router.get("/terminals.xlsx")
def terminals_xlsx(bucket: str = "", status: str = "", rm: str = "", dc: str = "", lho: str = "", q: str = "",
                   me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    rows = _filtered(T.rows(db, _scope(me)), bucket, status, rm, dc, lho, q)
    wb = Workbook()
    ws = wb.active
    ws.title = "Terminals"
    head = ["Days left", "Terminal expiry", "Status", "CSP", "KO code", "Terminal ID", "RM", "DC", "LHO", "Branch",
            "Documents ready", "Reason", "Agreement ends", "PVR ends", "IIBF ends", "LHO email", "RM follow-up",
            "Renewed"]
    ws.append(head)
    fill = {"expired": "F8CBAD", "d15": "FCE4D6", "d30": "FFF2CC"}
    for r in rows:
        dd = r["docs"]
        msg = lambda m: "" if not m else (f"sent {m['sent_at'][:10]}" if m["sent_at"] else m["status"].replace("_", " ").lower())
        ws.append([r["days_left"], r["expiry_date"], r["status_label"], r["csp"]["name"] if r["csp"] else r["name_in_report"],
                   r["ko_code"], r["terminal_id"], r["rm"], r["dc"], r["lho"], r["branch"],
                   "Yes" if r["ready"] else "No", r["reason"],
                   *[(dd.get(t) or {}).get("expiry") or (dd.get(t) or {}).get("status", "") for t in T.REQUIRED_TYPES],
                   msg(r["lho_email"]), msg(r["rm_followup"]),
                   f"{r['renewed']['from']} → {r['renewed']['to']}" if r["renewed"] else ""])
        if r["bucket"] in fill:
            for c in ws[ws.max_row][:3]:
                c.fill = PatternFill("solid", fgColor=fill[r["bucket"]])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for col, w in zip("ABCDEFGHIJKLMNOPQR", (9, 13, 24, 28, 12, 14, 18, 18, 16, 18, 10, 46, 13, 13, 13, 16, 16, 24)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"terminals_{date.today():%Y%m%d}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------ Bank reports (admins)
@router.get("/bank-reports", dependencies=[Depends(require_admin)])
def bank_reports(db: Session = Depends(get_db)):
    imports = db.query(TerminalImport).order_by(TerminalImport.uploaded_at.desc()).limit(30).all()
    not_linked = (db.query(Terminal).filter(Terminal.csp_id.is_(None))
                  .order_by(Terminal.expiry_date.is_(None), Terminal.expiry_date).limit(300).all())
    contacts = {c.name_key: c for c in db.query(LhoContact)}
    seen: Counter = Counter()
    names: dict[str, str] = {}
    for t, circle in db.query(Terminal, CSP.circle).outerjoin(CSP, CSP.id == Terminal.csp_id):
        n = (t.lho or circle or "").strip()
        if n:
            seen[T.lho_key(n)] += 1
            names.setdefault(T.lho_key(n), n)
    keys = sorted(set(seen) | set(contacts), key=lambda k: (-seen.get(k, 0), k))
    iso = lambda v: v.isoformat() if v else None
    return {
        "settings": T.settings(db),
        "imports": [{"id": i.id, "file_name": i.file_name, "report_date": iso(i.report_date), "uploaded_by": i.uploaded_by,
                     "uploaded_at": iso(i.uploaded_at), "rows": i.rows_read, "linked": i.rows_linked,
                     "not_linked": i.rows_not_linked, "skipped": i.rows_skipped, "renewed": i.renewed,
                     "columns": i.columns or {}, "problems": (i.problems or [])[:10]} for i in imports],
        "not_linked": [{"id": t.id, "terminal_id": t.terminal_id, "ko_code": t.ko_code, "name": t.name_in_report,
                        "lho": t.lho, "expiry_date": iso(t.expiry_date), "state": t.link_state} for t in not_linked],
        "lhos": [{"key": k, "name": (contacts[k].name if k in contacts else names.get(k, k)), "terminals": seen.get(k, 0),
                  "emails": (contacts[k].emails or []) if k in contacts else [],
                  "cc": (contacts[k].cc or []) if k in contacts else [],
                  "active": contacts[k].active if k in contacts else True,
                  "verified_by": contacts[k].verified_by if k in contacts else None,
                  "verified_at": iso(contacts[k].verified_at) if k in contacts else None} for k in keys],
    }


@router.post("/terminals/import", dependencies=[Depends(require_admin)])
async def import_terminal_report(file: UploadFile = File(...), report_date: str = Form(""),
                                 me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    data = await file.read(MAX_REPORT_BYTES + 1)
    if not data:
        raise HTTPException(422, "The file is empty.")
    if len(data) > MAX_REPORT_BYTES:
        raise HTTPException(413, "The file is larger than 15 MB.")
    try:
        day = date.fromisoformat(report_date) if report_date else date.today()
    except ValueError:
        raise HTTPException(422, "The report date is not a date.")
    if day > date.today():
        raise HTTPException(422, "The report date can't be in the future.")
    try:
        out = T.import_report(db, data, file.filename or "", day, me.name)
    except ValueError as e:
        db.rollback()
        raise HTTPException(422, str(e))
    out["agent"] = T.run(db)          # emails due because of this report, at once
    return out


@router.post("/terminals/{terminal_pk}/link", dependencies=[Depends(require_admin)])
def link_terminal(terminal_pk: int, code: str = Body(..., embed=True), me: Principal = Depends(require_user),
                  db: Session = Depends(get_db)):
    t = db.get(Terminal, terminal_pk)
    if t is None:
        raise HTTPException(404, "terminal not found")
    try:
        c = T.link_by_hand(db, t, code, me.name)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "csp": {"id": c.id, "name": c.name, "code": c.current_code}}


@router.post("/lho-contacts", dependencies=[Depends(require_admin)])
def save_lho(name: str = Body(...), emails: list[str] = Body(default=[]), cc: list[str] = Body(default=[]),
             active: bool = Body(True), me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    import re
    key = T.lho_key(name)
    if not key:
        raise HTTPException(422, "The LHO needs a name.")
    clean = lambda xs: [e for e in dict.fromkeys(x.strip().lower() for x in xs if x and x.strip())]
    emails, cc = clean(emails), clean(cc)
    bad = [e for e in emails + cc if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[a-z]{2,}", e)]
    if bad:
        raise HTTPException(422, f"Not an email address: {', '.join(bad)}")
    c = db.query(LhoContact).filter(LhoContact.name_key == key).first()
    if c is None:
        c = LhoContact(name=name.strip(), name_key=key)
        db.add(c)
    changed = (c.emails or []) != emails or (c.cc or []) != cc
    c.emails, c.cc, c.active = emails, cc, bool(active)
    if changed or c.verified_at is None:
        # Saving the addresses on this page is the admin's confirmation that they are the LHO's own.
        c.verified_by, c.verified_at = (me.name, datetime.now(timezone.utc).replace(tzinfo=None)) if emails else (None, None)
    db.commit()
    return {"ok": True, "name": c.name, "emails": c.emails, "cc": c.cc, "verified_by": c.verified_by}


@router.post("/terminals/settings", dependencies=[Depends(require_admin)])
def agent2_settings(body: dict = Body(...), me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    try:
        s = T.set_settings(db, body, me.name)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    if s["on"]:
        s["run"] = T.run(db)
    return s


@router.post("/terminals/run", dependencies=[Depends(require_admin)])
def agent2_run(db: Session = Depends(get_db)):
    return T.run(db)
