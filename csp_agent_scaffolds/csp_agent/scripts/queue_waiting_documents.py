"""
Documents waiting for a person (status NEEDS_REVIEW or NEEDS_APPROVAL) but
with no item in the Review queue, so nobody could check them:
  - the copy that counts for the CSP ("being checked"), and
  - uploads that don't count yet, when the CSP has no valid copy of that
    document (if a valid copy came since, they are only listed).
This adds the missing Review queue items. Dry run first:

    .venv/bin/python -m scripts.queue_waiting_documents
    .venv/bin/python -m scripts.queue_waiting_documents --apply

Nothing is accepted or rejected, and no CSP is messaged.
"""
import argparse

from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        waiting = {i for (i,) in db.query(ManualReviewQueue.document_id).filter(ManualReviewQueue.status == ReviewStatus.PENDING)}
        from app.compliance import canonical_type, evaluate
        rows = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
                .filter(CSP.is_active_in_calling_sheet.is_(True),
                        Document.status.in_([DocumentStatus.NEEDS_REVIEW, DocumentStatus.NEEDS_APPROVAL]))
                .order_by(CSP.current_code, Document.uploaded_at).all())
        missing, replaced, states = [], [], {}
        for d, c in rows:
            if d.id in waiting:
                continue
            t = canonical_type(d.document_type)
            if not d.is_current and t in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE"):
                st = states.setdefault(c.id, evaluate(db, c)).docs[t]
                if st.status == "VALID" and not st.in_review:
                    replaced.append((d, c))          # a valid copy counts now: nothing to decide
                    continue
            missing.append((d, c))
        if replaced:
            print(f"Already replaced by a valid copy (only listed, nothing to do): {len(replaced)}")
            for d, c in replaced:
                print(f"  {c.current_code}  {c.name[:26]:26}  {d.document_type:20}  uploaded {d.uploaded_at:%d-%m-%Y}")
            print("To add to the Review queue:")
        for d, c in missing:
            print(f"  {c.current_code}  {c.name[:26]:26}  {d.document_type:20}  {d.status.value:15} issued {d.issue_date} "
                  f"expires {d.expiry_date}  rule {d.validity_rule_used}  via {d.upload_channel}")
            if a.apply:
                why = ("dates read by the AI model" if d.status == DocumentStatus.NEEDS_APPROVAL
                       else (d.extracted_fields or {}).get("needs_confirmation") or "it waited for a person")
                db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason=(
                    f"Waiting to be checked ({why}) but was missing from the Review queue: check the dates and "
                    "accept, or correct / reject.")[:500]))
        if a.apply:
            db.commit()
            print(f"\n{len(missing)} document(s) added to the Review queue.")
        else:
            db.rollback()
            print(f"\n{len(missing)} document(s) shown as 'being checked' with no Review queue item. "
                  "Dry run: nothing changed. Add --apply to add them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
