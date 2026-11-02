"""
app/upload_jobs.py
Reading the upload page's documents after the quick checks, so a slow read
(OCR of a 13-page agreement) never makes Nginx answer "gateway time-out".

POST /api/portal/upload does the quick checks (link, size, file type, blurry
photo), keeps each document in an UploadJob and waits UPLOAD_WAIT_SECONDS
for it to be read here. Most are read by then and the page gets the answer as
before. A slower one answers "received, being checked" with the job's key;
the page asks GET /api/portal/upload-status until it is done, and the
WhatsApp reply goes to the CSP either way.

Read by UPLOAD_READERS threads in the web app. A job the web app could not
finish (restarted while reading) is picked up by the worker (sweep(), every
2 minutes). A job that fails MAX_ATTEMPTS times is FAILED: its documents go
to a person (Review queue), so the CSP doesn't have to send them again.
"""
import asyncio
import logging
import secrets
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from typing import Callable, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from .config import UPLOAD_READERS
from .models import UploadJob, UploadJobFile

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STALE_RUNNING = timedelta(minutes=15)     # RUNNING this long: the reader died (restart), read it again
STALE_QUEUED = timedelta(minutes=2)       # QUEUED this long: the web app never started it
_pool = ThreadPoolExecutor(max_workers=max(1, UPLOAD_READERS), thread_name_prefix="upload-reader")


def enqueue(db: Session, csp_id: int, token: Optional[str], channel: str, results: list[dict],
            files: list[dict], contact: Optional[dict]) -> UploadJob:
    job = UploadJob(key=secrets.token_urlsafe(16), csp_id=csp_id, token=token, channel=channel,
                    results=results, contact=contact)
    db.add(job)
    db.flush()
    for f in files:
        db.add(UploadJobFile(job_id=job.id, **f))
    return job


def claim(db: Session, job_id: int) -> bool:
    """Take the job for this reader; False if another has it (or it is done)."""
    now = func.now()
    n = (db.query(UploadJob)
         .filter(UploadJob.id == job_id, UploadJob.attempts < MAX_ATTEMPTS,
                 or_(UploadJob.status == "QUEUED",
                     and_(UploadJob.status == "RUNNING", UploadJob.claimed_at < now - STALE_RUNNING)))
         .update({UploadJob.status: "RUNNING", UploadJob.claimed_at: now,
                  UploadJob.attempts: UploadJob.attempts + 1}, synchronize_session=False))
    db.commit()
    return n == 1


def run(job_id: int, session_factory: Callable[[], Session]) -> None:
    """Read one job's documents and answer it (app/api/portal.py: _read_job)."""
    from .api.portal import _give_up_job, _read_job
    db = session_factory()
    try:
        if not claim(db, job_id):
            return
        job = db.get(UploadJob, job_id)
        try:
            _read_job(db, job)
            return
        except Exception as e:
            db.rollback()
            logger.exception("upload_job_failed job=%s", job_id)
            job = db.get(UploadJob, job_id)
            job.error = f"{type(e).__name__}: {e}"[:500]
            if job.attempts < MAX_ATTEMPTS:
                job.status = "QUEUED"                      # read again by the next reader
                db.commit()
                start(job_id, session_factory)
                return
            db.commit()
        _give_up_job(db, job)
    finally:
        db.close()


def start(job_id: int, session_factory: Callable[[], Session]) -> Future:
    return _pool.submit(run, job_id, session_factory)


async def wait(fut: Future, seconds: float) -> bool:
    """True if the job was read within `seconds` (it goes on being read either way)."""
    try:
        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


def reply(db: Session, key: str) -> Optional[dict]:
    """{"done": False} while it is read, the page's answer when done, None if no such job."""
    # Fresh from the database: the reader wrote it in its own session.
    job = db.query(UploadJob).filter(UploadJob.key == key).populate_existing().first()
    if job is None:
        return None
    if job.status in ("DONE", "FAILED") and job.reply is not None:
        return {"done": True, **job.reply}
    return {"done": False}


def sweep(session_factory: Callable[[], Session]) -> dict:
    """Worker, every 2 minutes: jobs left unread (the web app restarted) are read here."""
    db = session_factory()
    try:
        now = func.now()
        ids = [i for (i,) in db.query(UploadJob.id).filter(
            UploadJob.attempts < MAX_ATTEMPTS,
            or_(and_(UploadJob.status == "QUEUED", UploadJob.created_at < now - STALE_QUEUED),
                and_(UploadJob.status == "RUNNING", UploadJob.claimed_at < now - STALE_RUNNING)))
            .order_by(UploadJob.id).limit(20)]
        stuck = [i for (i,) in db.query(UploadJob.id).filter(
            UploadJob.attempts >= MAX_ATTEMPTS, UploadJob.status.in_(("QUEUED", "RUNNING")),
            or_(UploadJob.claimed_at.is_(None), UploadJob.claimed_at < now - STALE_RUNNING)).limit(20)]
    finally:
        db.close()
    for i in ids:
        run(i, session_factory)
    from .api.portal import _give_up_job
    for i in stuck:
        db = session_factory()
        try:
            _give_up_job(db, db.get(UploadJob, i))
        finally:
            db.close()
    return {"read": len(ids), "given_to_a_person": len(stuck)}
