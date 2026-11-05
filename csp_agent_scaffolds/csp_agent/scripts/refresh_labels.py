"""
Bring every CSP's saved Label and sub-group in line with its documents now.
Needed once after the upgrade to live Labels (app/live_labels.py): uploads
from the upload links and Gmail did not refresh the saved Label before, so a
CSP could sit in the Label 3 tab while its row read "2.2 PVR pending" until
the night's job. From now on every save refreshes it; this fixes the past.

    .venv/bin/python -m scripts.refresh_labels                 # list who would move, change nothing
    .venv/bin/python -m scripts.refresh_labels --csp 1A850978  # one CSP
    .venv/bin/python -m scripts.refresh_labels --apply

Only the saved Label, sub-group and reason change (and a document whose date
has passed is marked expired, as the nightly job does). No message is sent.
"""
import argparse
from collections import Counter

from app.compliance import evaluate, refresh_category, sub_slab
from app.db import SessionLocal
from app.models import CSP


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        q = db.query(CSP)
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        moves = Counter()
        n = changed = 0
        for csp in q.order_by(CSP.current_code).all():
            n += 1
            state = evaluate(db, csp)
            sub = sub_slab(state)
            if (csp.category, csp.sub_slab) == (state.category, sub):
                continue
            changed += 1
            moves[(csp.category, state.category)] += 1
            print(f"  {csp.current_code or '-':10}  {(csp.name or '')[:28]:28}  "
                  f"saved Label {csp.category} ({csp.sub_slab or '-'})  ->  now Label {state.category} ({sub})")
            if a.apply:
                refresh_category(db, csp)
        print()
        for (old, new), k in sorted(moves.items(), key=lambda x: (x[0][0] or 0, x[0][1])):
            print(f"  Label {old} -> Label {new}: {k}")
        if a.apply:
            db.commit()
            print(f"\n{changed} of {n} CSP(s) updated. The dashboard shows them in their right Label now.")
        else:
            db.rollback()
            print(f"\n{changed} of {n} CSP(s) have an out-of-date saved Label or sub-group. "
                  f"Dry run: nothing changed. Add --apply to update them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
