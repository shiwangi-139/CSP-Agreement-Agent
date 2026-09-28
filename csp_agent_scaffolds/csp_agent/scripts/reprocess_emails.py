"""
Read again, with the current rules, the Gmail emails whose attachments were
never stored: attachments rejected as "not a permitted document" (often
English OCR failing, e.g. before the tessdata fix) or errors, and emails no
CSP matched. Files already stored for any CSP are left exactly as they are
(scripts/check_document_owners.py and scripts/reextract_documents.py handle
those), so this never creates a second copy.

    python -m scripts.reprocess_emails            # dry run: writes logs/reprocess_plan_*.csv
    python -m scripts.reprocess_emails --apply    # read those emails again

Gmail is only read, never changed. Takes the same lock as the worker's
Gmail scan, so the two never run at once.
"""
import argparse
import csv
import logging
from datetime import datetime
from pathlib import Path

from app.db import SessionLocal
from app.models import InboundMessage

RETRY = {"NOT_ALLOWED", "ERROR"}


def candidates(db) -> list[InboundMessage]:
    out = []
    for m in db.query(InboundMessage).order_by(InboundMessage.received_at):
        if m.status == "UNMATCHED_NO_CSP" or any(
                (a.get("decision") in RETRY) for a in (m.attachment_decisions or [])):
            out.append(m)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    db = SessionLocal()
    try:
        rows = candidates(db)
        out = Path("logs") / f"reprocess_plan_{datetime.now():%Y%m%d_%H%M%S}.csv"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["message_id", "received", "status", "subject", "before"])
            for m in rows:
                before = "; ".join(f"{a.get('filename')}={a.get('decision')}" for a in (m.attachment_decisions or []))
                w.writerow([m.external_message_id, m.received_at, m.status, m.subject, before or m.error_message])
        unmatched = sum(m.status == "UNMATCHED_NO_CSP" for m in rows)
        print(f"{len(rows)} emails to read again: {len(rows) - unmatched} with rejected attachments, "
              f"{unmatched} with no CSP matched. Plan: {out}")
        if not args.apply:
            print("Dry run. Run again with --apply.")
            return

        from app import gmail_ingest
        from app.comms.gmail_oauth import get_gmail_service
        from app.worker import advisory_lock
        with advisory_lock("gmail-scan") as got:
            if not got:
                print("The worker's Gmail scan is running right now; try again in a few minutes.")
                return
            service = get_gmail_service()
            result: dict = {}
            for i, m in enumerate(rows, 1):
                ext = m.external_message_id
                try:
                    db.delete(m)
                    db.flush()
                    status = gmail_ingest.process_message(db, service, ext, known_anywhere=True)
                    db.commit()
                except Exception as e:
                    db.rollback()  # the old record comes back
                    status = "ERROR"
                    print(f"  {ext}: failed ({type(e).__name__}: {e}); left as it was")
                result[status] = result.get(status, 0) + 1
                if i % 25 == 0:
                    print(f"  {i}/{len(rows)} {result}")
            print(f"done: {result}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
