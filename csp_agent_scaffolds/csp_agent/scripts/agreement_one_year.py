"""
Agreements still on the OLD 2-year default (AGREEMENT_DEFAULT_2_YEAR: read
before 30-09-2026, when an agreement with no validity written counted 2
years) get today's rule. Each is first re-read with the current rules, which
catch more wordings ("Initial Term ... three (3) years", printed end dates):
a validity written in it is used. Only when none is written: 1 year from the
issue date (AGREEMENT_DEFAULT_YEARS). Dry run first:

    .venv/bin/python -m scripts.agreement_one_year                    # list, change nothing
    .venv/bin/python -m scripts.agreement_one_year --csp 1A850687     # one CSP
    .venv/bin/python -m scripts.agreement_one_year --no-reread        # faster: 1 year, no re-reading
    .venv/bin/python -m scripts.agreement_one_year --apply

With --apply the dates are saved and each CSP's label is worked out again.
An agreement whose 1 year is over becomes expired: the daily run (09:00)
treats it like any other expired agreement. This script sends no message.
"""
import argparse
from datetime import date

from app import vault
from app.compliance import refresh_category
from app.db import SessionLocal
from app.document_service import _d, recompute_current, sync_agreement_row
from app.expiry_engine import AGREEMENT_DEFAULT_YEARS, add_years
from app.models import CSP, Document, DocumentStatus

OLD_RULE = "AGREEMENT_DEFAULT_2_YEAR"
NEW_RULE = f"AGREEMENT_DEFAULT_{AGREEMENT_DEFAULT_YEARS}_YEAR"


def _reread(doc: Document) -> dict:
    """The current rules' reading when it finds a validity written in the agreement, else {}."""
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    path = vault.abs_path(doc.storage_path) if doc.storage_path else None
    if path is None or not path.exists():
        return {}
    ex = extract_document_fields_deterministic(path.read_bytes(), path.name, allow_llm_fallback=False,
                                               expected_type="AGREEMENT")
    if ex.get("document_type") != "AGREEMENT" or ex.get("readability") != "READABLE" or not ex.get("expiry_date") \
            or (ex.get("validity_rule_used") or "").startswith("AGREEMENT_DEFAULT"):
        return {}
    return ex


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    ap.add_argument("--no-reread", action="store_true", help="skip re-reading the files (1 year for all)")
    a = ap.parse_args()
    today = date.today()
    db = SessionLocal()
    try:
        q = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
             .filter(Document.document_type == "AGREEMENT", Document.validity_rule_used == OLD_RULE))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        rows = q.order_by(CSP.current_code, Document.id).all()
        stated = expired = undated = 0
        touched = {}
        for doc, csp in rows:
            label = f"  {csp.current_code}  {csp.name[:24]:24}  doc {doc.id}"
            ex = {} if a.no_reread else _reread(doc)
            if ex:
                issue, expiry, rule = _d(ex.get("start_date")), _d(ex.get("expiry_date")), ex.get("validity_rule_used")
                stated += 1
            elif doc.issue_date:
                issue, expiry, rule = doc.issue_date, add_years(doc.issue_date, AGREEMENT_DEFAULT_YEARS), NEW_RULE
            else:
                undated += 1
                print(f"{label}  no issue date: left for a person")
                continue
            now = "EXPIRED now" if expiry < today else f"valid {(expiry - today).days} more days"
            expired += expiry < today
            print(f"{label}  {doc.issue_date} -> {doc.expiry_date}  becomes  {issue} -> {expiry}  ({rule}; {now})"
                  f"{'  (on file now)' if doc.is_current else ''}")
            if a.apply:
                doc.issue_date, doc.expiry_date, doc.validity_rule_used = issue, expiry, rule
                doc.validity_months = ex.get("validity_months") if ex else 12 * AGREEMENT_DEFAULT_YEARS
                doc.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause")) if ex else False
                if doc.status in (DocumentStatus.VALID, DocumentStatus.EXPIRED):
                    doc.status = DocumentStatus.EXPIRED if expiry < today else DocumentStatus.VALID
                doc.extracted_fields = dict(doc.extracted_fields or {}, start_date=issue.isoformat(),
                                            expiry_date=expiry.isoformat(), reread=f"was {OLD_RULE}")
                touched[csp.id] = csp
        if a.apply:
            db.flush()
            for csp in touched.values():
                recompute_current(db, csp, "AGREEMENT")
                sync_agreement_row(db, csp)
                refresh_category(db, csp)
            db.commit()
            print(f"\n{len(touched)} CSP(s) changed: {stated} with a validity written in the agreement, "
                  f"the rest 1 year from issue; {expired} now expired. {undated} without an issue date left as they were.")
        else:
            db.rollback()
            print(f"\n{len(rows)} agreement(s) on the old 2-year default: {stated} have a validity written "
                  f"(re-read), {expired} would be expired, {undated} have no issue date. "
                  "Dry run: nothing changed. Add --apply to save.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
