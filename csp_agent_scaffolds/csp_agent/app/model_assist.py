"""
app/model_assist.py
The local AI model (Ollama) as a careful helper for the Review queue.

  - It only reads documents already waiting for a person (the rules couldn't
    read them). Uploads and Gmail scans never ask it.
  - It suggests (issue date, type, validity it saw); the reviewer decides.
    Nothing is accepted, rejected or sent to a CSP because of it.
  - At most MODEL_ASSIST_DAILY_LIMIT documents a day, one at a time, only in
    MODEL_ASSIST_HOURS, model unloaded after every answer.
  - Ollama runs only while it works: started for the batch, stopped after.
  - Memory guard (also every minute, job "model_guard"): when the server's
    RAM use passes MODEL_MAX_SERVER_RAM_PERCENT, or this project's Ollama
    passes MODEL_MAX_OLLAMA_GB, every Ollama process of this project
    (OLLAMA_BIN, same user) is stopped, and killed if it doesn't stop.
    Nothing else on the server is touched.
"""
import logging
import os
import signal
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from .config import (LOCAL_VLM_URL, MODEL_ASSIST, MODEL_ASSIST_DAILY_LIMIT, MODEL_ASSIST_HOURS,
                     MODEL_MAX_OLLAMA_GB, MODEL_MAX_SERVER_RAM_PERCENT, OLLAMA_BIN, OLLAMA_LOG, OLLAMA_MODELS_DIR)
from .models import Document, DocumentStatus, ManualReviewQueue, ReviewStatus

logger = logging.getLogger(__name__)
PROC = Path("/proc")


# ------------------------------------------------------------ memory guard
def server_ram_percent(meminfo: Path = PROC / "meminfo") -> Optional[float]:
    """Share of the server's RAM in use (MemTotal - MemAvailable), or None."""
    try:
        vals = {}
        for line in meminfo.read_text().splitlines():
            k, v = line.split(":", 1)
            vals[k] = int(v.split()[0])
        return round(100 * (vals["MemTotal"] - vals["MemAvailable"]) / vals["MemTotal"], 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def own_ollama_pids() -> list[int]:
    """This project's Ollama processes: started from OLLAMA_BIN (serve and
    its model runners) by this same user. Never anyone else's."""
    target = os.path.realpath(OLLAMA_BIN)
    me = os.getuid()
    pids = []
    for p in PROC.iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            if p.stat().st_uid != me:
                continue
            argv0 = (p / "cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
        except OSError:
            continue
        if argv0 and os.path.realpath(argv0) == target:
            pids.append(int(p.name))
    return pids


def ollama_rss_gb(pids: list[int]) -> float:
    total_kb = 0
    for pid in pids:
        try:
            for line in (PROC / str(pid) / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
        except OSError:
            continue
    return round(total_kb / 1048576, 2)


def stop_ollama(reason: str) -> list[int]:
    """Stop every Ollama process of this project (SIGTERM, then SIGKILL)."""
    pids = own_ollama_pids()
    if not pids:
        return []
    logger.warning("ollama_stopping pids=%s reason=%s", pids, reason)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    for _ in range(20):
        time.sleep(0.5)
        if not [p for p in pids if (PROC / str(p)).exists()]:
            break
    for pid in [p for p in pids if (PROC / str(p)).exists()]:
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning("ollama_killed pid=%s", pid)
        except OSError:
            pass
    return pids


def too_heavy() -> Optional[str]:
    """Why Ollama must not run now (or must be stopped), or None."""
    ram = server_ram_percent()
    if ram is not None and ram > MODEL_MAX_SERVER_RAM_PERCENT:
        return f"server RAM {ram}% > {MODEL_MAX_SERVER_RAM_PERCENT}%"
    gb = ollama_rss_gb(own_ollama_pids())
    if gb > MODEL_MAX_OLLAMA_GB:
        return f"Ollama uses {gb} GB > {MODEL_MAX_OLLAMA_GB} GB"
    return None


def guard() -> dict:
    """Every minute: stop this project's Ollama if the server is under memory pressure."""
    pids = own_ollama_pids()
    if not pids:
        return {"ollama": "not running"}
    why = too_heavy()
    if why:
        return {"ollama": "stopped", "why": why, "pids": stop_ollama(why)}
    return {"ollama": "running", "ram_percent": server_ram_percent(), "ollama_gb": ollama_rss_gb(pids)}


# ------------------------------------------------------------ start / reach
def _reachable() -> bool:
    import socket
    u = urlparse(LOCAL_VLM_URL or "http://127.0.0.1:11434")
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 11434), timeout=2):
            return True
    except OSError:
        return False


def start_ollama() -> bool:
    """Start this project's Ollama (lowest CPU priority, one request at a
    time, model unloaded after each answer) unless it is already up."""
    if _reachable():
        return True
    if not os.path.exists(OLLAMA_BIN) or not LOCAL_VLM_URL:
        logger.warning("ollama_not_started bin=%s url=%s", OLLAMA_BIN, LOCAL_VLM_URL)
        return False
    u = urlparse(LOCAL_VLM_URL)
    env = dict(os.environ, OLLAMA_MODELS=OLLAMA_MODELS_DIR, OLLAMA_KEEP_ALIVE="0", OLLAMA_NUM_PARALLEL="1",
               OLLAMA_MAX_LOADED_MODELS="1", OLLAMA_HOST=f"{u.hostname or '127.0.0.1'}:{u.port or 11434}")
    with open(OLLAMA_LOG, "a") as log:          # the child keeps its own copy of the file
        subprocess.Popen(["nice", "-n", "19", OLLAMA_BIN, "serve"], env=env, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        time.sleep(1)
        if _reachable():
            logger.info("ollama_started")
            return True
    logger.warning("ollama_did_not_start")
    return False


# ------------------------------------------------------------ the batch
def in_hours(now: datetime, hours: str = MODEL_ASSIST_HOURS) -> bool:
    try:
        a, b = (int(x) for x in hours.split("-"))
    except ValueError:
        return False
    h = now.hour
    return a <= h < b if a < b else (h >= a or h < b)


def used_today(db: Session, today: date) -> int:
    n = 0
    for (f,) in db.query(Document.extracted_fields).filter(Document.extracted_fields.isnot(None),
                                                          Document.extracted_fields.has_key("model_suggestion")):
        if str(((f or {}).get("model_suggestion") or {}).get("at", ""))[:10] == today.isoformat():
            n += 1
    return n


def _waiting(db: Session) -> list[tuple[ManualReviewQueue, Document]]:
    rows = (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
            .filter(ManualReviewQueue.status == ReviewStatus.PENDING, Document.status == DocumentStatus.NEEDS_REVIEW)
            .order_by(ManualReviewQueue.created_at).all())
    return [(q, d) for q, d in rows if not (q.reason or "").startswith(("Spot check", "Reference check"))
            and "model_suggestion" not in (d.extracted_fields or {})]


def suggestion_from(ans: Optional[dict]) -> dict:
    ans = ans or {}
    out = {"provider": ans.get("provider"), "legible": ans.get("is_legible"),
           "document_type": ans.get("document_type"), "issue_date": ans.get("issue_date"),
           "evidence": (ans.get("issue_date_evidence") or "")[:160]}
    if ans.get("three_year_validity_stated"):
        out["validity_months"] = 36
    elif ans.get("validity_months_stated") in (6, 12):
        out["validity_months"] = ans["validity_months_stated"]
    return out


def run_batch(db: Session, now: Optional[datetime] = None) -> dict:
    from . import vault
    from .ai.extraction.deterministic_extractor import suggest_with_local_model
    now = now or datetime.now()
    if not MODEL_ASSIST:
        return {"skipped": "MODEL_ASSIST is off"}
    if not in_hours(now):
        return {"skipped": f"outside {MODEL_ASSIST_HOURS}"}
    left = MODEL_ASSIST_DAILY_LIMIT - used_today(db, now.date())
    todo = _waiting(db)[:max(left, 0)]
    if not todo:
        return {"done": 0, "left_today": max(left, 0)}
    why = too_heavy()
    if why:
        stop_ollama(why)
        return {"skipped": why}
    if not start_ollama():
        return {"skipped": "Ollama could not start"}
    done = 0
    try:
        for q, d in todo:
            why = too_heavy()
            if why:
                stop_ollama(why)
                return {"done": done, "stopped": why}
            path = vault.abs_path(d.storage_path) if d.storage_path else None
            if path is None or not path.exists():
                continue
            ans = suggest_with_local_model(path.read_bytes(), d.document_type or "UNKNOWN")
            d.extracted_fields = {**(d.extracted_fields or {}),
                                  "model_suggestion": {**suggestion_from(ans), "at": datetime.now().isoformat(timespec="seconds")}}
            db.commit()
            done += 1
    finally:
        # Not left running between batches.
        stop_ollama("review helper batch finished")
    return {"done": done}
