"""
Re-read documents that were stored as UNREADABLE, e.g. after the local
vision model (Ollama) is installed on the rack server. The files are already
kept in each CSP's unreadable/ folder, so Gmail is not read again. A copy that
is now readable is updated in place and moved out of unreadable/.

    python -m scripts.retry_unreadable            # dry run: show what would change
    python -m scripts.retry_unreadable --apply    # store the newly readable ones
"""
import argparse
import logging

from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import refresh_category
from app.db import SessionLocal
from app import vault
from app.compliance import canonical_type
from app.document_service import _d, _status_for, recompute_current, sync_agreement_row
from app.models import CSP, Document


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=1000)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    db = SessionLocal()
    fixed = still = 0
    try:
        rows = db.query(Document).filter(Document.readability == "UNREADABLE").limit(args.limit).all()
        print(f"{len(rows)} unreadable documents to retry")
        for d in rows:
            path = vault.abs_path(d.storage_path)
            if path is None or not path.exists():
                continue
            data = path.read_bytes()
            ex = extract_document_fields_deterministic(data, d.original_filename or path.name)
            csp = db.get(CSP, d.csp_id)
            print(f"{csp.current_code} {d.document_type:20} -> {ex['readability']:11} "
                  f"issue={ex.get('start_date')} src={ex.get('date_source')} {ex.get('model_provider') or ''}")
            if ex["readability"] != "READABLE":
                still += 1
                continue
            fixed += 1
            if args.apply:
                d.readability, d.status = "READABLE", _status_for(ex)
                d.document_type = canonical_type(ex.get("document_type")) or d.document_type
                d.issue_date, d.expiry_date = _d(ex.get("start_date")), _d(ex.get("expiry_date"))
                d.validity_rule_used, d.date_source = ex.get("validity_rule_used"), ex.get("date_source")
                d.validity_months = ex.get("validity_months")
                d.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause"))
                d.holder_name, d.iibf_reg_number = ex.get("holder_name"), ex.get("iibf_registration_number")
                d.extraction_method = ex.get("extraction_method")
                d.extracted_fields = {**(d.extracted_fields or {}), "retried_readable": True,
                                      "model_provider": ex.get("model_provider")}
                recompute_current(db, csp, d.document_type)
                sync_agreement_row(db, csp)
                vault.place_csp(db, csp)
                refresh_category(db, csp)
                db.commit()
        print(f"now readable: {fixed}, still unreadable: {still}" + ("" if args.apply else "  (dry run: nothing saved)"))
    finally:
        db.close()


if __name__ == "__main__":
    main()
