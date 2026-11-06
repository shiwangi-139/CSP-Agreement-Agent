"""
Documents filed under the wrong CSP (app/owner_guard.py). Mostly emails that
carried several CSPs' agreements, each named with its KO code
("1A850074 ASHWANI KUMAR.pdf"), all filed under the one CSP the email matched;
some were then accepted by a person who was only asked about the dates.
Every route is covered (Gmail, the CSP's link, the common link, the DC link),
accepted documents too.

    .venv/bin/python -m scripts.fix_misfiled_documents                 # dry run: Excel list in logs/, nothing changed
    .venv/bin/python -m scripts.fix_misfiled_documents --csp 1A851437  # only documents filed under / belonging to one CSP
    .venv/bin/python -m scripts.fix_misfiled_documents --apply
    sudo .venv/bin/python -m scripts.fix_misfiled_documents --suggest           # list suggested owners
    sudo .venv/bin/python -m scripts.fix_misfiled_documents --suggest --apply   # save them on the Review items

--suggest: for the Review items asking whose a document is (and those whose
owner could not be confirmed), the CSP the name and the address on the
document point to, with the reasons (app/owner_guard.py suggest_owner). When
another CSP's FULL NAME and DISTRICT are on it, and neither this CSP's full
name nor its district is ("AUTO"), --apply also moves it to that CSP, or
rejects it here if they already have the same file. It reads the files
(sudo: they belong to the app).

What --apply does:
  MOVE    the document shows exactly one other CSP's KO code (in it or in its
          file name) and none of this CSP's codes, old codes included: it is
          filed under that CSP and waits in the Review queue there ("Moved
          from ...: check once"); it counts for them once accepted
  REJECT  as MOVE, but that CSP already has the same file: rejected here
  ASK     shows several other CSPs' codes, or the same file is on several
          CSPs' records and no code says whose: a Review queue item asks a
          person; nothing else changes
The CSP a counted document is taken from drops to its right Label at once and
is asked for its own document by the usual reminders. No message is sent here.
"""
import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from app.owner_guard import (KO, codes_in_filename, codes_shown, csp_by_code, decisive_other, holders_of_same_file,
                              move_document, other_owners, own_codes, suggest_owner)

ASK_PREFIX = "Whose document is it?"
BY = "fix_misfiled_documents"


def _who(c: CSP) -> str:
    return f"{c.current_code} {c.name}"


def plan(db, only: str = "") -> list[dict]:
    by_id = {c.id: c for c in db.query(CSP).all()}
    docs = db.query(Document).filter(Document.status != DocumentStatus.REJECTED).order_by(Document.id).all()
    rows, planned = [], set()
    for d in docs:
        others = other_owners(db, d)
        if not others:
            continue
        csp = by_id[d.csp_id]
        in_file = set(codes_in_filename(d.original_filename))
        if len(others) == 1:
            to = others[0]
            where = "in its file name" if to.current_code in in_file else "read in the document"
            why = f"The document shows {_who(to)} ({where}), not {_who(csp)}."
            same = db.query(Document.id).filter(Document.csp_id == to.id, Document.sha256 == d.sha256).first()
            action = "REJECT" if same else "MOVE"
        else:
            to = None
            why = "The document shows several other CSPs: " + ", ".join(_who(o) for o in others) + "."
            action = "ASK"
        rows.append({"action": action, "doc": d, "csp": csp, "to": to, "why": why})
        planned.add(d.id)
    # The same file on several CSPs' records with no code saying whose.
    groups = defaultdict(list)
    for d in docs:
        groups[d.sha256].append(d)
    for sha, same in groups.items():
        if len({d.csp_id for d in same}) < 2:
            continue
        codes = {d.csp_id: by_id[d.csp_id].current_code for d in same}
        for d in same:
            if d.id in planned or d.status == DocumentStatus.UNREADABLE:
                continue
            csp = by_id[d.csp_id]
            if own_codes(db, csp) & set(codes_shown(d)):
                continue                               # shows this CSP's own code: it is theirs
            if db.query(ManualReviewQueue.id).filter(ManualReviewQueue.document_id == d.id,
                                                     ManualReviewQueue.status == ReviewStatus.PENDING).first():
                continue                               # a person looks at it already
            rest = ", ".join(sorted(v for k, v in codes.items() if k != d.csp_id))
            rows.append({"action": "ASK", "doc": d, "csp": csp, "to": None,
                         "why": f"The same file is also on record for {rest}; no KO code on it says whose."})
    if only:
        code = only.strip().upper()
        rows = [r for r in rows if r["csp"].current_code == code or (r["to"] and r["to"].current_code == code)]
    return rows


def write_excel(rows: list[dict]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Misfiled documents"
    ws.append(["Action", "Document", "Type", "Status", "Counted now", "Filed under", "Belongs to", "Why",
               "File name", "Came by", "Uploaded"])
    for c in ws[1]:
        c.font = Font(name="Arial", bold=True)
    for r in rows:
        d = r["doc"]
        ws.append([r["action"], d.id, d.document_type, d.status.name, "yes" if d.is_current else "no", _who(r["csp"]),
                   _who(r["to"]) if r["to"] else "", r["why"], d.original_filename or "", d.upload_channel or "",
                   d.uploaded_at.strftime("%d-%m-%Y %H:%M") if d.uploaded_at else ""])
    for col, w in zip("ABCDEFGHIJK", (9, 10, 20, 16, 11, 30, 30, 70, 50, 18, 17)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = Font(name="Arial")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out = Path("logs") / f"misfiled_documents_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    out.parent.mkdir(exist_ok=True)
    wb.save(out)
    return out


def apply(db, rows: list[dict]) -> dict:
    done = defaultdict(int)
    for r in rows:
        d = r["doc"]
        if r["action"] in ("MOVE", "REJECT"):
            done[move_document(db, d, r["to"], BY, r["why"])] += 1
        else:
            db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason=(
                f"{ASK_PREFIX} {r['why']} Reject it here if it isn't {r['csp'].name}'s document.")[:500]))
            done["ASKED"] += 1
        db.commit()
    return dict(done)


def suggest_pending(db, apply_: bool, only: str = "") -> int:
    """Suggested owners for the waiting "whose document is it?" items."""
    from app import vault
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    rows = (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
            .filter(ManualReviewQueue.status == ReviewStatus.PENDING).order_by(Document.id).all())
    from app.gmail_ingest import _letters
    from app.renewal_engine import run_for_csp
    everyone = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).all()
    n, auto = 0, defaultdict(int)
    for q, d in rows:
        f = d.extracted_fields or {}
        asking = (q.reason or "").startswith(ASK_PREFIX)
        if not (asking or f.get("owner_check")) or f.get("owner_suggestion"):
            continue
        csp = db.get(CSP, d.csp_id)
        if only and csp.current_code != only.strip().upper():
            continue
        path = vault.abs_path(d.storage_path)
        if path is None or not path.exists():
            print(f"  doc {d.id}: file not found ({d.storage_path})")
            continue
        ex = extract_document_fields_deterministic(path.read_bytes(), d.original_filename or "", allow_llm_fallback=False)
        tokens = set(_letters(ex.get("match_text")).split())
        named = [o for o in everyone if o.id != csp.id and len(ws := {w for w in _letters(o.name).split() if len(w) >= 3}) >= 2
                 and ws <= tokens][:8]
        cands = [csp, *holders_of_same_file(db, d, csp.id),
                 *(csp_by_code(db, c) for c in dict.fromkeys(KO.findall(f.get("owner_check") or ""))), *named]
        sug = suggest_owner(ex.get("match_text"), cands)
        if not sug:
            continue
        n += 1
        best, other = sug["best"], decisive_other(sug, csp.id)
        if other is not None:
            has = db.query(Document.id).filter(Document.csp_id == other["id"], Document.sha256 == d.sha256).first()
            act = "AUTO REJECT here" if has else "AUTO MOVE"
            auto[act] += 1
        else:
            act = "suggest"
        print(f"  {act:16} doc {d.id:<6} on {_who(csp)[:30]:30} -> "
              f"{(best['code'] + ' ' + best['name']) if best else 'no clear owner'}")
        for c in sug["candidates"]:
            print(f"        {c['code']} {c['name']}: {c['why']}")
        if apply_:
            d.extracted_fields = {**f, "owner_suggestion": sug}
            if other is not None:
                to = db.get(CSP, other["id"])
                move_document(db, d, to, BY, f"Its full name and district are {_who(to)}'s, not {_who(csp)}'s "
                                             f"({other['why']}).")
                run_for_csp(db, csp)            # asked for their own document (drafts only, as the daily run)
            db.commit()
    if auto:
        print(f"\nDecided by name + district (another CSP's full name and district on it, not this CSP's): "
              + ", ".join(f"{k}: {v}" for k, v in auto.items()))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="", help="only documents filed under, or belonging to, this KO code")
    ap.add_argument("--suggest", action="store_true", help="suggested owners for the waiting Review items")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.suggest:
            n = suggest_pending(db, a.apply, a.csp)
            if a.apply:
                print(f"\n{n} suggestion(s) saved: they show on the Review items.")
            else:
                db.rollback()
                print(f"\n{n} suggestion(s). Dry run: nothing saved. Add --apply to show them on the Review items.")
            return
        rows = plan(db, a.csp)
        for r in rows:
            d = r["doc"]
            print(f"  {r['action']:6} doc {d.id:<6} {d.document_type:20} {'COUNTED ' if d.is_current else ''}"
                  f"{_who(r['csp'])[:32]:32} -> {_who(r['to'])[:32] if r['to'] else 'a person decides':32}  {d.original_filename or ''}")
        n = defaultdict(int)
        for r in rows:
            n[r["action"]] += 1
        counted = len({r["csp"].id for r in rows if r["doc"].is_current and r["action"] != "ASK"})
        out = write_excel(rows)
        print(f"\n{len(rows)} document(s): {n['MOVE']} to move, {n['REJECT']} to reject (owner already has the file), "
              f"{n['ASK']} for a person to decide. {counted} CSP(s) lose a document counted as theirs. List: {out}")
        if a.apply:
            print("Done:", apply(db, rows))
        else:
            db.rollback()
            print("Dry run: nothing changed. Check the list, then run again with --apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
