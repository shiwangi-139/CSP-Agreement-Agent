"""
Put back in the Review queue the agreements a person accepted although no
validity or end date is written in them (their validity is still the
default rule, AGREEMENT_DEFAULT_*), e.g. accepted by mistake, so they can be
viewed again and rejected. Dry run first:

    .venv/bin/python -m scripts.reopen_accepted_agreements              # list, last 7 days
    .venv/bin/python -m scripts.reopen_accepted_agreements --days 14
    .venv/bin/python -m scripts.reopen_accepted_agreements --csp 1A850687
    .venv/bin/python -m scripts.reopen_accepted_agreements --apply

With --apply each one goes back to "being checked" (the CSP gets no
reminder for it meanwhile) with a new Review queue item. Nothing is
rejected and no CSP is messaged: the person decides in the Review queue.
"""
import argparse
from datetime import datetime, timedelta, timezone

from app.compliance import refresh_category
from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus

REASON = ("Reopened: this agreement was accepted on {when}, but no validity or end date is written in it "
          "(only the default rule). View it: reject it if the CSP must upload a new agreement, or accept it again.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--csp", default="")
    a = ap.parse_args()
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=a.days)
    db = SessionLocal()
    try:
        q = (db.query(ManualReviewQueue, Document, CSP)
             .join(Document, Document.id == ManualReviewQueue.document_id).join(CSP, CSP.id == Document.csp_id)
             .filter(ManualReviewQueue.status == ReviewStatus.APPROVED, ManualReviewQueue.resolved_at >= since,
                     Document.document_type == "AGREEMENT", Document.status == DocumentStatus.MANUAL_VERIFIED,
                     Document.validity_rule_used.like("AGREEMENT_DEFAULT%")))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        seen = set()
        rows = []
        for item, doc, csp in q.order_by(ManualReviewQueue.resolved_at).all():
            if doc.id not in seen:
                seen.add(doc.id)
                rows.append((item, doc, csp))
        for item, doc, csp in rows:
            print(f"  {csp.current_code}  {csp.name[:26]:26}  doc {doc.id}  issued {doc.issue_date}  "
                  f"expires {doc.expiry_date}  accepted {item.resolved_at:%d-%m %H:%M} {item.correction_notes or ''}")
            if a.apply:
                doc.status = DocumentStatus.NEEDS_REVIEW
                db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING,
                                         reason=REASON.format(when=f"{item.resolved_at:%d-%m-%Y}")))
                db.flush()
                refresh_category(db, csp)
        if a.apply:
            db.commit()
            print(f"\n{len(rows)} agreement(s) back in the Review queue. Reject or accept them on the dashboard.")
        else:
            db.rollback()
            print(f"\n{len(rows)} agreement(s) found. Dry run: nothing changed. Add --apply to put them back.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
