"""
Re-read stored documents with the CURRENT rules and update their dates in
place. Use after a rule is improved. Gmail is not read again.

    python -m scripts.reextract_documents --code 1A850376          # one CSP, dry run
    python -m scripts.reextract_documents --type AGREEMENT          # all agreements, dry run
    python -m scripts.reextract_documents --type AGREEMENT --apply  # save changes

Only changes to issue date, expiry, validity rule or status are written;
everything is printed first (and saved to logs/reextract_*.csv) so you can
check it. Documents a reviewer accepted, corrected or rejected, and copies
held because their owner is unconfirmed, are left alone.
"""
import argparse
import csv
import logging
from datetime import date, datetime
from pathlib import Path

from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import refresh_category
from app.db import SessionLocal
from app import vault
from app.document_service import _d, _status_for, recompute_current, sync_agreement_row
from app.models import CSP, Document, DocumentStatus

KEEP = {DocumentStatus.MANUAL_VERIFIED, DocumentStatus.REJECTED}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", help="only this CSP code")
    ap.add_argument("--type", help="AGREEMENT, POLICE_VERIFICATION or IIBF_CERTIFICATE")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    db = SessionLocal()
    changed = same = skipped = 0
    try:
        q = db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id).filter(Document.readability == "READABLE")
        if args.code:
            q = q.filter(CSP.current_code == args.code.upper())
        if args.type:
            q = q.filter(Document.document_type == args.type.upper())
        rows = q.order_by(CSP.current_code).all()
        print(f"{len(rows)} documents to re-check" + ("" if args.apply else " (dry run)"))
        report = []
        for d, csp in rows:
            if d.status in KEEP or (d.extracted_fields or {}).get("owner_check"):
                skipped += 1
                continue
            path = vault.abs_path(d.storage_path)
            if path is None or not path.exists():
                skipped += 1
                continue
            ex = extract_document_fields_deterministic(path.read_bytes(), d.original_filename or path.name)
            if ex["readability"] != "READABLE" or ex["document_type"] != d.document_type:
                print(f"  {csp.current_code} {d.document_type}: now {ex['readability']} {ex['document_type']} — left unchanged")
                skipped += 1
                continue
            new = {"issue_date": _d(ex.get("start_date")), "expiry_date": _d(ex.get("expiry_date")),
                   "validity_rule_used": ex.get("validity_rule_used"), "status": _status_for(ex)}
            old = {k: getattr(d, k) for k in new}
            if old == new:
                same += 1
                continue
            changed += 1
            fmt = lambda v: v.isoformat() if isinstance(v, date) else getattr(v, "value", v)
            diff = ", ".join(f"{k}: {fmt(old[k])} -> {fmt(new[k])}" for k in new if old[k] != new[k])
            print(f"  {csp.current_code} {csp.name[:24]:24} {d.document_type}: {diff}")
            report.append({"document_id": d.id, "csp_code": csp.current_code, "csp_name": csp.name,
                           "type": d.document_type, "change": diff, "file": d.storage_path})
            if args.apply:
                for k, v in new.items():
                    setattr(d, k, v)
                d.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause"))
                d.validity_months = ex.get("validity_months")
                d.date_source = ex.get("date_source")
                d.extracted_fields = {**(d.extracted_fields or {}), "reextracted_on": date.today().isoformat()}
                recompute_current(db, csp, d.document_type)
                sync_agreement_row(db, csp)
                refresh_category(db, csp)
                vault.place_csp(db, csp)  # the dates are in the file name
                db.commit()
        out = Path("logs") / f"reextract_{datetime.now():%Y%m%d_%H%M%S}.csv"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["document_id", "csp_code", "csp_name", "type", "change", "file"])
            w.writeheader()
            w.writerows(report)
        print(f"report: {out}")
        print(f"changed: {changed}, unchanged: {same}, skipped: {skipped}"
              + ("" if args.apply else "  — dry run, run again with --apply to save"))
    finally:
        db.close()


if __name__ == "__main__":
    main()
