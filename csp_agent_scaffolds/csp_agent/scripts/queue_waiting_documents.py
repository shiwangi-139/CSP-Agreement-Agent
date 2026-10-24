"""
Documents shown as "being checked" (status NEEDS_REVIEW or NEEDS_APPROVAL,
the copy that counts for the CSP) but with no item waiting in the Review
queue: nobody could check them, and the CSP got no reminder for them. This
adds the missing Review queue items. Dry run first:

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
        rows = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
                .filter(Document.is_current.is_(True), CSP.is_active_in_calling_sheet.is_(True),
                        Document.status.in_([DocumentStatus.NEEDS_REVIEW, DocumentStatus.NEEDS_APPROVAL]))
                .order_by(CSP.current_code).all())
        missing = [(d, c) for d, c in rows if d.id not in waiting]
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
