"""
Police Clearance Certificates accepted with "no expiry" (PVR_PCC_LIFETIME,
chosen by a reviewer until 02-11-2026) become valid 1 year from their issue
date, like every police certificate with no validity written. Dry run first:

    .venv/bin/python -m scripts.pcc_one_year                  # list, change nothing
    .venv/bin/python -m scripts.pcc_one_year --csp 1A850687   # one CSP
    .venv/bin/python -m scripts.pcc_one_year --apply

With --apply each certificate gets its 1-year expiry and each CSP's label is
worked out again. A certificate issued more than a year ago is then expired:
the CSP needs a new police certificate, and the daily run (09:00) treats it
like any other expired one (reminders, lists). No message is sent by this
script. A certificate with no issue date is only listed: a person sets it.
"""
import argparse
from datetime import date

from app.compliance import refresh_category
from app.db import SessionLocal
from app.document_service import recompute_current
from app.expiry_engine import ExpiryRule, add_years
from app.models import CSP, Document


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    a = ap.parse_args()
    today = date.today()
    db = SessionLocal()
    try:
        q = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
             .filter(Document.validity_rule_used == ExpiryRule.PVR_PCC_LIFETIME))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        rows = q.order_by(CSP.current_code, Document.issue_date).all()
        expired = undated = 0
        csps = {}
        for doc, csp in rows:
            if doc.issue_date is None:
                undated += 1
                print(f"  {csp.current_code}  {csp.name[:26]:26}  doc {doc.id}  no issue date: set it in the Review queue")
                continue
            new = add_years(doc.issue_date, 1)
            now = "EXPIRED now" if new < today else f"valid {(new - today).days} more days"
            expired += new < today
            print(f"  {csp.current_code}  {csp.name[:26]:26}  doc {doc.id}  issued {doc.issue_date:%d-%m-%Y}  "
                  f"expires {new:%d-%m-%Y}  {now}{'  (on file now)' if doc.is_current else ''}")
            if a.apply:
                doc.validity_months, doc.expiry_date = 12, new
                doc.validity_rule_used = ExpiryRule.PVR_PCC_DEFAULT_1_YEAR
                csps[csp.id] = csp
        if a.apply:
            db.flush()
            for csp in csps.values():
                recompute_current(db, csp, "POLICE_VERIFICATION")
                refresh_category(db, csp)
            db.commit()
            print(f"\n{len(csps)} CSP(s) changed: {len(rows) - undated} certificate(s) now 1 year from issue, "
                  f"{expired} of them expired. {undated} without an issue date left as they were.")
        else:
            db.rollback()
            print(f"\n{len(rows)} certificate(s) with no expiry; {expired} would be expired at 1 year from issue, "
                  f"{undated} have no issue date. Dry run: nothing changed. Add --apply to change them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
