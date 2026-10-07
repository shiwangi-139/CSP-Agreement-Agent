"""
Move every stored document into the vault layout (app/vault.py) and report
the plan first. Nothing is ever deleted.

    python -m scripts.reorganize_vault                       # dry run: writes logs/vault_plan_*.csv
    python -m scripts.reorganize_vault --apply               # move files, update the database
    python -m scripts.reorganize_vault --today 2027-06-01    # dry run as if it were that day
                                                             # (which files would become EXPIRED)

What --apply does:
  1. For each CSP: pick the current copy per document type, refresh expiry,
     and move/rename its files (one CSP per transaction; if saving fails,
     that CSP's files are moved back).
  2. Files under the old storage/ folders that no database row points to are
     moved to <vault parent>/legacy/:
        duplicates/   same bytes as a document already in the vault
        unreferenced/ everything else, keeping its old sub-path
  3. Old folders left empty are removed.
Stop the web server and the worker before running with --apply.
"""
import argparse
import csv
import hashlib
import logging
import shutil
from datetime import date, datetime
from pathlib import Path

from app import vault
from app.compliance import REQUIRED_TYPES, refresh_category
from app.db import SessionLocal
from app.document_service import recompute_current, sync_agreement_row
from app.models import CSP, Document

OLD_ROOTS = [vault.PROJECT_ROOT / "storage", vault.PROJECT_ROOT / "scripts" / "storage"]


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(p: Path, root: Path) -> bool:
    p, root = p.resolve(), root.resolve()
    return p == root or root in p.parents


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--today", help="YYYY-MM-DD, dry run only")
    args = ap.parse_args()
    if args.apply and args.today:
        ap.error("--today is for dry runs only")
    today = date.fromisoformat(args.today) if args.today else date.today()
    logging.basicConfig(level=logging.WARNING)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    logs = vault.PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    plan_path = logs / f"vault_plan_{stamp}{'' if args.apply else '_dryrun'}.csv"
    legacy = vault.ROOT.parent / "legacy"
    rows = []

    db = SessionLocal()
    try:
        # ---- 1. documents the database knows about
        ids = [i for (i,) in db.query(Document.csp_id).distinct()]
        csps = db.query(CSP).filter(CSP.id.in_(ids)).order_by(CSP.current_code).all() if ids else []
        missing = 0
        for csp in csps:
            for t in REQUIRED_TYPES:
                recompute_current(db, csp, t)
            sync_agreement_row(db, csp)
            refresh_category(db, csp, today)
            db.flush()
            for d in db.query(Document).filter(Document.csp_id == csp.id, Document.storage_path.isnot(None)):
                p = vault.abs_path(d.storage_path)
                if p is None or not p.exists():
                    missing += 1
                    rows.append(["MISSING_FILE", d.id, csp.current_code, d.storage_path, ""])
            if not args.apply:
                for old, new in vault.place_csp(db, csp, today, dry_run=True):
                    rows.append(["MOVE", "", csp.current_code, old, new])
                db.rollback()
                continue
            done = []
            try:
                for d in sorted(db.query(Document).filter(Document.csp_id == csp.id,
                                                          Document.storage_path.isnot(None)).all(),
                                key=lambda d: (bool(d.is_current), d.id)):
                    src = vault.abs_path(d.storage_path)
                    m = vault.place(db, d, csp, today)
                    if m:
                        done.append((src, vault.abs_path(d.storage_path)))
                        rows.append(["MOVE", d.id, csp.current_code, m[0], m[1]])
                db.commit()
            except Exception as e:
                db.rollback()
                for src, dst in reversed(done):
                    if dst is not None and dst.exists() and src is not None and not src.exists():
                        src.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dst), str(src))
                rows.append(["FAILED_CSP", "", csp.current_code, str(e)[:200], "files moved back"])
                print(f"  {csp.current_code}: failed ({e}); its files were moved back")

        # ---- 2. files no row points to
        known = {}
        for d in db.query(Document).filter(Document.storage_path.isnot(None)):
            p = vault.abs_path(d.storage_path)
            if p is not None and p.exists():
                known[p.resolve()] = d.sha256
        in_vault_hashes = set(known.values())
        # Never swept: the OCR cache and reports (they sit next to the vault,
        # which may itself be inside storage/), files being received, and the
        # vault's own index and marker. Leftover copies inside the vault (old
        # code-only folders such as 1A850168/) are swept like any other.
        keep = [vault.ROOT.parent / "ocr_cache", vault.ROOT.parent / "reports", legacy, vault.ROOT / vault.STAGING,
                vault.ROOT / vault.PORTAL_REJECTED]
        orphans = 0
        for root in OLD_ROOTS + [vault.ROOT]:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if not f.is_file() or any(_inside(f, k) for k in keep):
                    continue
                if _inside(f, vault.ROOT) and root != vault.ROOT:
                    continue  # handled in the vault's own pass
                if f.parent == vault.ROOT and (f.name in ("INDEX.xlsx", vault.MARKER) or f.name.startswith(".~lock")):
                    continue
                if f.resolve() in known:
                    continue
                orphans += 1
                kind = "duplicates" if _sha(f) in in_vault_hashes else "unreferenced"
                dest = legacy / kind / f.relative_to(root.parent if root != vault.ROOT else vault.ROOT.parent)
                rows.append([f"LEGACY_{kind.upper()}", "", "", str(f.relative_to(vault.PROJECT_ROOT)),
                             str(dest.relative_to(vault.PROJECT_ROOT)) if _inside(dest, vault.PROJECT_ROOT) else str(dest)])
                if args.apply:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest = dest.with_name(f"{dest.stem}_{_sha(f)[:8]}{dest.suffix}")
                    shutil.move(str(f), str(dest))

        # ---- 3. empty old folders
        if args.apply:
            for root in OLD_ROOTS + [vault.ROOT]:
                if not root.is_dir():
                    continue
                for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    if d != vault.ROOT and not any(_inside(d, k) for k in keep) and not any(d.iterdir()):
                        d.rmdir()
            vault.write_index(db, today)
    finally:
        db.close()

    with open(plan_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "document_id", "csp_code", "from", "to"])
        w.writerows(rows)
    moves = sum(1 for r in rows if r[0] == "MOVE")
    print(f"CSPs: {len(csps)}   file moves: {moves}   legacy files: {orphans}   rows with missing files: {missing}")
    print(f"vault: {vault.ROOT}")
    print(f"plan:  {plan_path}")
    if not args.apply:
        print("dry run: nothing was moved and the database was not changed. Run again with --apply.")


if __name__ == "__main__":
    main()
