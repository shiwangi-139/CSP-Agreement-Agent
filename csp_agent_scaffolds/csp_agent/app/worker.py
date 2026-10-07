"""
app/worker.py
The autonomous agent. Run it as its own process on the rack server:

    python -m app.worker

The web server (uvicorn) no longer starts any jobs, so running several web
workers can't multiply them. Each job also takes a Postgres advisory lock,
so even two worker processes never run the same job at the same time.

Schedule (Asia/Kolkata):
  Gmail scan (backfill chunk, then only new mail) every GMAIL_SCAN_INTERVAL_MINUTES
  Calling-sheet reload                             every 60 minutes
  Categories + renewal ladders + upload cycle      daily 09:00
  Outbox sender / retries                          every 2 minutes
  Vault: expiry renames + INDEX.xlsx               daily 00:10
  Excel reports (CSP report, Contacts & gaps)      daily 07:30
Missed runs (server off) are run once when it comes back (coalesce + 6 h grace).
"""
import logging
import signal
import zlib
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import text

from .config import (APP_TIMEZONE, FEATURE_MESSAGING_ANALYTICS, GMAIL_SCAN_INTERVAL_MINUTES, VAULT_JOB_HOUR,
                     VAULT_JOB_MINUTE)
from .db import SessionLocal, engine
from .logging_config import configure_logging

logger = logging.getLogger("app.worker")
LAST_RUNS: dict[str, dict] = {}


@contextmanager
def advisory_lock(name: str):
    key = zlib.crc32(f"csp-agent:{name}".encode())
    with engine.connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


def _run(name: str, fn):
    with advisory_lock(name) as got:
        if not got:
            logger.info("job_skipped_already_running job=%s", name)
            return {"skipped": "already running elsewhere"}
        started = datetime.now()
        try:
            result = fn()
            LAST_RUNS[name] = {"status": "OK", "at": started.isoformat(timespec="seconds"), "result": result}
            logger.info("job_done job=%s result=%s", name, result)
            return result
        except Exception as e:
            LAST_RUNS[name] = {"status": "FAILED", "at": started.isoformat(timespec="seconds"), "error": str(e)}
            logger.exception("job_failed job=%s", name)
            raise


def job_gmail():
    from .gmail_ingest import run_scan
    return _run("gmail", lambda: run_scan(max_messages=200))


def job_sheet():
    from .comms.sheets_sync import sync_calling_sheet
    return _run("sheet", sync_calling_sheet)


def job_engine():
    from . import renewal_engine

    def go():
        db = SessionLocal()
        try:
            return renewal_engine.run(db)
        finally:
            db.close()
    return _run("engine", go)


def job_outbox():
    from .comms.outbound import auto_approve, process_outbox

    def go():
        db = SessionLocal()
        try:
            approved = auto_approve(db)
            return {**process_outbox(db), **({"auto_approved": approved} if approved else {})}
        finally:
            db.close()
    return _run("outbox", go)


def job_delivery():
    from .comms.outbound import refresh_whatsapp_delivery

    def go():
        db = SessionLocal()
        try:
            return refresh_whatsapp_delivery(db)
        finally:
            db.close()
    return _run("delivery", go)


def job_vault():
    from . import vault

    def go():
        db = SessionLocal()
        try:
            return vault.run_nightly(db)
        finally:
            db.close()
    return _run("vault", go)


def job_reports():
    from . import reports

    def go():
        db = SessionLocal()
        try:
            return reports.write_daily(db)
        finally:
            db.close()
    return _run("reports", go)


def build_scheduler():
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    sched = BlockingScheduler(timezone=APP_TIMEZONE,
                              job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600})
    sched.add_job(job_gmail, IntervalTrigger(minutes=GMAIL_SCAN_INTERVAL_MINUTES), id="gmail",
                  next_run_time=datetime.now())
    sched.add_job(job_sheet, IntervalTrigger(minutes=60), id="sheet", next_run_time=datetime.now())
    sched.add_job(job_engine, CronTrigger(hour=9, minute=0), id="engine")
    sched.add_job(job_outbox, IntervalTrigger(minutes=2), id="outbox")
    if FEATURE_MESSAGING_ANALYTICS:                       # parked feature
        sched.add_job(job_delivery, IntervalTrigger(minutes=30), id="delivery")
    sched.add_job(job_vault, CronTrigger(hour=VAULT_JOB_HOUR, minute=VAULT_JOB_MINUTE), id="vault")
    sched.add_job(job_reports, CronTrigger(hour=7, minute=30), id="reports")
    return sched


def main():
    configure_logging()
    from .ai.providers.vision_fallback import check_groq_vision_model
    check_groq_vision_model()
    from .auth import warn_if_open
    warn_if_open()
    from . import vault
    vault.check_root()
    logger.info("vault_root %s", vault.ROOT)
    sched = build_scheduler()
    signal.signal(signal.SIGTERM, lambda *_: sched.shutdown(wait=False))
    logger.info("worker_started timezone=%s jobs=%s", APP_TIMEZONE, [j.id for j in sched.get_jobs()])
    sched.start()


if __name__ == "__main__":
    main()
