"""
scripts/shrink_documents.py
Make large scanned documents smaller without changing what they say
(app/shrink.py). A DRY RUN by default: nothing in the vault or the
database changes; logs/shrink_report.csv says, file by file, how much
smaller it would be and whether the reader gets the same answers.

    cd /home/shiwangi/csp_agent_scaffolds/csp_agent
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.shrink_documents              # 20 largest agreements
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.shrink_documents --limit 50
    sudo nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.shrink_documents --apply --limit 20

--apply replaces only files whose smaller copy reads exactly the same; each
original is kept in storage/documents/_originals/<date>/ for 30 days.
--nightly is for the worker: it does nothing unless SHRINK_NIGHTLY is on,
then applies to at most SHRINK_PER_NIGHT files within --minutes.
"""
import argparse
import csv
import time
from pathlib import Path

from app import shrink
from app.config import SHRINK_MIN_MB, SHRINK_NIGHTLY, SHRINK_PER_NIGHT, SHRINK_TYPES
from app.db import SessionLocal


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="really replace the files that read the same")
    ap.add_argument("--limit", type=int, default=20, help="how many files to try")
    ap.add_argument("--type", action="append", help="document type (default: SHRINK_TYPES, AGREEMENT)")
    ap.add_argument("--min-mb", type=float, default=SHRINK_MIN_MB)
    ap.add_argument("--doc", type=int, action="append", help="only these document ids")
    ap.add_argument("--nightly", action="store_true")
    ap.add_argument("--minutes", type=int, default=60, help="stop starting new files after this long")
    a = ap.parse_args()
    if a.nightly:
        if not SHRINK_NIGHTLY:
            print("SHRINK_NIGHTLY is off: nothing done.")
            return
        a.apply, a.limit = True, SHRINK_PER_NIGHT
    db = SessionLocal()
    started = time.time()
    try:
        cands = shrink.candidates(db, types=tuple(a.type or SHRINK_TYPES), min_mb=a.min_mb, only=a.doc)
        total_mb = sum(c[2] for c in cands) / 1048576
        print(f"{len(cands)} file(s) of {a.min_mb} MB or more ({total_mb:.1f} MB in all). "
              f"Trying {min(a.limit, len(cands))}{'' if a.apply else ' (dry run: nothing changes)'}.", flush=True)
        Path("logs").mkdir(exist_ok=True)
        rows = []
        with open("logs/shrink_report.csv", "w", newline="") as f:
            w = None
            for d, path, size in cands[:a.limit]:
                if time.time() - started > a.minutes * 60:
                    print("Time is up: stopping here.")
                    break
                row = shrink.try_one(db, d, path, apply=a.apply)
                rows.append(row)
                if w is None:
                    w = csv.DictWriter(f, fieldnames=list(row))
                    w.writeheader()
                w.writerow(row)
                f.flush()
                print(f"{row['csp']} doc {row['document_id']}: {row['before_mb']} MB -> {row['after_mb'] or '-'} MB  "
                      f"{row['action']}{'  [' + row['differs'] + ']' if row['differs'] else ''}", flush=True)
        ok = [r for r in rows if r["same_reading"]]
        differ = [r for r in rows if r["same_reading"] is False]
        before = sum(r["before_mb"] for r in ok)
        after = sum(r["after_mb"] for r in ok)
        print(f"\nChecked {len(rows)}: {len(ok)} read the same when smaller, {len(differ)} read differently "
              f"(kept as they are), {len(rows) - len(ok) - len(differ)} skipped.")
        if ok:
            saving = 1 - after / before                       # on the files that read the same
            share = len(ok) / (len(ok) + len(differ))         # how many read the same
            print(f"Those {len(ok)}: {before:.1f} MB -> {after:.1f} MB ({100 * saving:.0f}% smaller). "
                  f"At that rate all {len(cands)} would go from {total_mb:.1f} MB to about "
                  f"{total_mb * (1 - saving * share):.1f} MB.")
        print("Report: logs/shrink_report.csv" + ("" if a.apply else "   (dry run: nothing was changed)"))
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
