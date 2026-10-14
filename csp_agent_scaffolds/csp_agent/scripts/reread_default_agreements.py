"""
Re-read agreements that were stored with the 1-year default (no validity
found in the text) with the current rules, e.g. a printed "END AND CLOSE DATE
01-06-2028" or "Full 3 year agreement" that the older rules missed.

Only the dates of the stored copy change (issue date, expiry, rule); nothing
is deleted. A copy whose re-read gives the same answer, or no answer, is left
as it is. CSPs whose documents become complete get the usual WhatsApp reply.

    python -m scripts.reread_default_agreements                    # dry run: shows what would change
    python -m scripts.reread_default_agreements --csp 1A851804     # one CSP only
    python -m scripts.reread_default_agreements --apply
    python -m scripts.reread_default_agreements --apply --no-message
"""
import argparse

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import refresh_category
from app.db import SessionLocal
from app.document_service import _d, _status_for, recompute_current, sync_agreement_row
from app.models import CSP, Document

DEFAULT_RULE = "AGREEMENT_DEFAULT_1_YEAR"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="", help="only this CSP code, e.g. 1A851804")
    ap.add_argument("--no-message", action="store_true", help="don't send the CSPs their WhatsApp reply")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        q = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
             .filter(Document.document_type == "AGREEMENT", Document.validity_rule_used == DEFAULT_RULE))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        changed, touched = 0, set()
        for doc, csp in q.order_by(CSP.current_code, Document.id).all():
            label = f"{csp.current_code} {csp.name[:24]:24} doc {doc.id}"
            path = vault.abs_path(doc.storage_path) if doc.storage_path else None
            if path is None or not path.exists():
                print(f"{label} skip: file not found")
                continue
            ex = extract_document_fields_deterministic(path.read_bytes(), path.name, allow_llm_fallback=False,
                                                       expected_type="AGREEMENT")
            if ex.get("document_type") != "AGREEMENT" or ex.get("readability") != "READABLE" \
                    or ex.get("validity_rule_used") == DEFAULT_RULE or not ex.get("expiry_date"):
                print(f"{label} unchanged (still no validity found)")
                continue
            print(f"{label} {doc.issue_date} -> {doc.expiry_date}  becomes  "
                  f"{ex['start_date']} -> {ex['expiry_date']} ({ex['compliance_status']}, {ex['validity_rule_used']})")
            changed += 1
            if a.apply:
                doc.issue_date, doc.expiry_date = _d(ex.get("start_date")), _d(ex.get("expiry_date"))
                doc.validity_rule_used, doc.date_source = ex.get("validity_rule_used"), ex.get("date_source")
                doc.validity_months = ex.get("validity_months")
                doc.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause"))
                doc.status = _status_for(ex)
                doc.extracted_fields = dict(doc.extracted_fields or {}, start_date=ex.get("start_date"),
                                            expiry_date=ex.get("expiry_date"),
                                            reread=f"re-read: was {DEFAULT_RULE}")
                db.flush()
                recompute_current(db, csp, "AGREEMENT")
                sync_agreement_row(db, csp)
                vault.place_csp(db, csp)
                touched.add(csp.id)
        if a.apply:
            from app.renewal_engine import on_documents_received
            for cid in touched:
                on_documents_received(db, db.get(CSP, cid))
                refresh_category(db, db.get(CSP, cid))
            db.commit()
            if not a.no_message:
                from app.comms.upload_receipt import send_upload_receipt
                for cid in touched:
                    # Only "all done" goes out here (no upload results to report).
                    print(f"  message to {db.get(CSP, cid).current_code}: {send_upload_receipt(cid)}")
        else:
            db.rollback()
        print(f"\n{changed} agreement(s) {'updated' if a.apply else 'would change'}.")
        if not a.apply:
            print("dry run: nothing changed. Run with --apply to save.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
