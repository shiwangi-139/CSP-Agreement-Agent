"""
Run the 2-year Gmail backfill to completion (resumable), then stop.
    python -m scripts.run_backfill
Progress is saved after every page, so it can be stopped and restarted.
It only reads Gmail and never sends anything.
"""
import logging
import time

from app.db import SessionLocal
from app.gmail_ingest import run_scan, ingestion_status
from app.logging_config import configure_logging


def main():
    configure_logging()
    log = logging.getLogger("backfill")
    while True:
        summary = run_scan(max_messages=100)
        if summary.get("mode") == "SKIPPED":
            log.info("another Gmail scan is already running; this copy will exit.")
            return
        log.info("backfill_tick %s", {k: v for k, v in summary.items() if k != "state"})
        db = SessionLocal()
        try:
            st = ingestion_status(db)
        finally:
            db.close()
        log.info("backfill_progress %s", st)
        if st["state"].get("backfill_complete"):
            log.info("backfill_complete")
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
