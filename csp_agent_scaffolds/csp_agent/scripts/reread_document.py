"""
Read one stored document again with the current rules, e.g. an agreement
stored as unreadable months ago that today's rules can read. Dry run first:

    .venv/bin/python -m scripts.reread_document --id 7            # shows what the rules read now
    .venv/bin/python -m scripts.reread_document --id 7 --apply    # stores that reading

With --apply an unreadable copy that now reads is updated in place (the same
way as when the same file is sent again: app/document_service.py), with the
safety gate's risk check, and a Review queue item is added so a person
confirms it with Accept. The CSP's label and follow-ups are updated; no
message is sent. A readable document is only shown, never changed (use
"Re-read with current rules" in the Review queue for those). The AI model is
never asked.
"""
import argparse

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import canonical_type, refresh_category
from app.db import SessionLocal
from app.document_service import store_extracted_document
from app.models import CSP, Document, ManualReviewQueue, ReviewStatus

SHOW = ("readability", "document_type", "start_date", "expiry_date", "date_source", "validity_rule_used",
        "readability_reason", "type_review", "page_count", "pages_readable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, required=True, help="the document's number (#)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        d = db.get(Document, a.id)
        if d is None:
            raise SystemExit(f"No document #{a.id}.")
        csp = db.get(CSP, d.csp_id)
        path = vault.abs_path(d.storage_path)
        if path is None or not path.exists():
            raise SystemExit(f"The file of #{a.id} is not in the vault ({d.storage_path}).")
        print(f"#{d.id} {csp.current_code} {csp.name}: {d.document_type} stored as {d.status.value} / {d.readability}")
        ex = extract_document_fields_deterministic(path.read_bytes(), d.original_filename or path.name,
                                                   allow_llm_fallback=False,
                                                   expected_type=canonical_type(d.document_type) or None)
        print("Read now:", {k: ex.get(k) for k in SHOW})
        if d.readability != "UNREADABLE":
            print("It is stored as readable: nothing to do here (use 'Re-read with current rules' in the Review queue).")
            return
        if ex.get("readability") != "READABLE":
            print("Still not readable with the current rules: nothing changed.")
            return
        if not a.apply:
            print("Dry run: nothing saved. Add --apply to store this reading and put it in the Review queue.")
            return
        out = store_extracted_document(db, csp, path.read_bytes(), d.original_filename or path.name, d.mime_type, ex,
                                       channel=d.upload_channel)
        doc = out.document
        if not db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == doc.id,
                                                  ManualReviewQueue.status == ReviewStatus.PENDING).first():
            fmt = lambda v: "-".join(reversed(str(v)[:10].split("-"))) if v and v != "LIFETIME_NO_EXPIRY" else "no expiry"
            db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
                f"Read again with the current rules (it was unreadable): issued {fmt(ex.get('start_date'))}, valid till "
                f"{fmt(ex.get('expiry_date'))} ({ex.get('validity_rule_used')}). Check it on the document and Accept.")[:500]))
        from app.renewal_engine import on_documents_received
        on_documents_received(db, csp)
        state = refresh_category(db, csp)
        db.commit()
        print(f"Saved: #{doc.id} is now {doc.status.value}, in the Review queue for a person to accept. "
              f"{csp.current_code} is Label {state.category}: {state.reason}")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
