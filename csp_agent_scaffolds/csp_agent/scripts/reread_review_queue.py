"""
Read every document waiting in the Review queue again with the current
rules (no AI model), so reviewers see today's reading instead of the one
from when it arrived. Nothing is accepted, rejected or sent to a CSP: only
the dates and the note on each waiting item change, and a person decides.

    python -m scripts.reread_review_queue                  # dry run: what would change
    python -m scripts.reread_review_queue --apply
    python -m scripts.reread_review_queue --csp 1A850014   # one CSP
"""
import argparse

from app.db import SessionLocal
from app.review_tools import reread_waiting, waiting_items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        items = waiting_items(db, a.csp)
        changed = 0
        for q, d, c in items:
            r = reread_waiting(db, q, d)
            b, af = r.get("before") or {}, r.get("after")
            if r["changed"]:
                changed += 1
                print(f"{c.current_code} {c.name[:22]:22} {d.document_type[:6]:6} {b.get('issue')} -> {b.get('expiry')}"
                      f"   now {af['issue']} -> {af['expiry']} ({af['rule']})", flush=True)
            else:
                print(f"{c.current_code} {c.name[:22]:22} {d.document_type[:6]:6} unchanged", flush=True)
        if a.apply:
            db.commit()
        else:
            db.rollback()
        print(f"\n{len(items)} waiting item(s), {changed} {'updated' if a.apply else 'would change'}."
              + ("" if a.apply else " Dry run: nothing changed. Add --apply to save."))
    finally:
        db.close()


if __name__ == "__main__":
    main()
