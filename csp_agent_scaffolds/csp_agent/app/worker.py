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
  Daily Label snapshot (Monitoring page history)   daily 09:30 (and at start if missed)
  Review helper (local Ollama, capped, see app/model_assist.py)  hourly at :15
  Ollama memory guard (stops this project's Ollama)             every minute
  Campaign: RM / DC lists of pending CSPs (app/escalation.py)    daily 11:00 (weekly after it)
  Campaign: CSPs' short evening nudge                            daily 17:00
Missed runs while the worker was running late are run once (coalesce + 6 h
grace). Daily runs missed while it was stopped (09:00 run, 11:00 lists,
17:00 nudge) are run once when it starts again (job_catch_up). It is kept
running by scripts/keep_alive.sh (cron) and scripts/run_forever.sh.
"""
import logging
import signal
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

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
        started_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            result = fn()
            LAST_RUNS[name] = {"status": "OK", "at": started.isoformat(timespec="seconds"), "result": result}
            logger.info("job_done job=%s result=%s", name, result)
            _save_run(name, started_utc, "SKIPPED" if isinstance(result, dict) and result.get("skipped") else "OK",
                      result if isinstance(result, dict) else {"result": result})
            return result
        except Exception as e:
            LAST_RUNS[name] = {"status": "FAILED", "at": started.isoformat(timespec="seconds"), "error": str(e)}
            logger.exception("job_failed job=%s", name)
            _save_run(name, started_utc, "FAILED", {"error": f"{type(e).__name__}: {e}"[:500]})
            raise


def _save_run(name: str, started_utc: datetime, status: str, summary: dict) -> None:
    """Kept for the Monitoring page (the worker's memory is lost on restart)."""
    from .monitoring import record_run
    db = SessionLocal()
    try:
        record_run(db, f"job:{name}", started_utc, status, summary)
    finally:
        db.close()


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


def job_model_assist():
    from . import model_assist

    def go():
        db = SessionLocal()
        try:
            return model_assist.run_batch(db)
        finally:
            db.close()
    return _run("model_assist", go)


def job_snapshot():
    from . import monitoring

    def go():
        db = SessionLocal()
        try:
            return {"csps": monitoring.snapshot_labels(db)}
        finally:
            db.close()
    return _run("snapshot", go)


def job_snapshot_if_missing():
    """At start: take today's Label snapshot if the 09:30 one was missed."""
    from . import monitoring
    db = SessionLocal()
    try:
        missing = not monitoring.has_snapshot(db)
    finally:
        db.close()
    if missing:
        job_snapshot()


# Daily jobs to make up at start if today's run was missed: id -> (job, hour, minute).
CATCH_UP = {"engine": ("job_engine", 9, 0), "staff_lists": ("job_staff_lists", 11, 0), "nudges": ("job_nudges", 17, 0)}


def missed_today(db, now_local: datetime) -> list[str]:
    """Daily jobs whose time today has passed with no saved run since then."""
    from zoneinfo import ZoneInfo
    from .models import MonitorRun
    tz = ZoneInfo(APP_TIMEZONE)
    out = []
    for name, (_, hour, minute) in CATCH_UP.items():
        due = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now_local < due:
            continue
        due_utc = due.astimezone(timezone.utc).replace(tzinfo=None)
        ran = db.query(MonitorRun.id).filter(MonitorRun.kind == f"job:{name}", MonitorRun.started_at >= due_utc,
                                             MonitorRun.status.in_(["OK", "SKIPPED"])).first()
        if ran is None:
            out.append(name)
    return out


def job_catch_up():
    """At start: run today's daily jobs the worker missed while it was down
    (each is safe to run twice: drafts are made once a day)."""
    from zoneinfo import ZoneInfo
    db = SessionLocal()
    try:
        names = missed_today(db, datetime.now(ZoneInfo(APP_TIMEZONE)))
    finally:
        db.close()
    for name in names:
        logger.warning("catch_up job=%s (missed while the worker was stopped)", name)
        try:
            globals()[CATCH_UP[name][0]]()
        except Exception:
            logger.exception("catch_up_failed job=%s", name)


def job_model_guard():
    """Every minute, no lock or log unless it acts: cheap /proc reads."""
    from . import model_assist
    try:
        r = model_assist.guard()
        if r.get("ollama") == "stopped":
            logger.warning("model_guard %s", r)
    except Exception:
        logger.exception("model_guard_failed")


def job_staff_lists():
    from . import escalation

    def go():
        db = SessionLocal()
        try:
            return {**escalation.draft_staff_lists(db, "RM"), **escalation.draft_staff_lists(db, "DC")}
        finally:
            db.close()
    return _run("staff_lists", go)


def job_nudges():
    from . import escalation

    def go():
        db = SessionLocal()
        try:
            return escalation.draft_nudges(db)
        finally:
            db.close()
    return _run("nudges", go)


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
    sched.add_job(job_model_assist, CronTrigger(minute=15), id="model_assist")
    sched.add_job(job_snapshot, CronTrigger(hour=9, minute=30), id="snapshot")
    sched.add_job(job_snapshot_if_missing, id="snapshot_at_start", next_run_time=datetime.now())
    sched.add_job(job_catch_up, id="catch_up_at_start", next_run_time=datetime.now())
    sched.add_job(job_model_guard, IntervalTrigger(minutes=1), id="model_guard", next_run_time=datetime.now())
    sched.add_job(job_staff_lists, CronTrigger(hour=11, minute=0), id="staff_lists")
    sched.add_job(job_nudges, CronTrigger(hour=17, minute=0), id="nudges")
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
