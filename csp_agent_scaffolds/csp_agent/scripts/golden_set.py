"""
scripts/golden_set.py
The golden set: every document a person decided (accepted, corrected or
rejected in the Review queue), with the answer frozen as it is today. The
files stay where they are; only the list and its answers are saved
(table golden_cases), under a new version number. An old version is never
changed, so results stay comparable: "did today's rules get the same
documents right as last month's?"

    .venv/bin/python -m scripts.golden_set --freeze --by "Shiwangi"
    .venv/bin/python -m scripts.golden_set --list

Checked every night with the other reading checks
(scripts/eval_reading.py --golden, app/worker.py).
"""
import argparse
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import func

from app.db import SessionLocal
from app.models import GoldenCase


def latest_version(db) -> int:
    return db.query(func.max(GoldenCase.version)).scalar() or 0


def freeze(db, by: str) -> tuple[int, Counter]:
    from scripts.eval_reading import _truth
    version = latest_version(db) + 1
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    n = Counter()
    for d, _c, truth in _truth(db):
        db.add(GoldenCase(version=version, document_id=d.id, document_type=d.document_type, truth=truth,
                          issue_date=d.issue_date, expiry_date=d.expiry_date, frozen_at=now, frozen_by=by))
        n[(d.document_type, truth)] += 1
    db.commit()
    return version, n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true", help="save today's decided documents as a new version")
    ap.add_argument("--by", default="", help="who froze it (shown in the record)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        if a.freeze:
            v, n = freeze(db, a.by)
            print(f"Golden set version {v}: {sum(n.values())} documents")
            for (t, truth), k in sorted(n.items()):
                print(f"  {t:20} {truth:7} {k}")
        if a.list or not a.freeze:
            rows = (db.query(GoldenCase.version, func.count(GoldenCase.id), func.min(GoldenCase.frozen_at),
                             func.min(GoldenCase.frozen_by)).group_by(GoldenCase.version).order_by(GoldenCase.version))
            for v, k, at, by in rows:
                print(f"version {v}: {k} documents, frozen {at:%d-%m-%Y %H:%M} UTC{' by ' + by if by else ''}")
            if not latest_version(db):
                print("No golden set yet: run with --freeze.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
