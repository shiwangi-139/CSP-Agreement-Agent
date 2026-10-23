"""
app/monitoring.py
The admin "Monitoring" page: how the agent, the documents, the reading and
the messages are doing. It watches the system and the work queues, not
people, and has no server section (memory, disk) on purpose.

Also keeps the history it needs:
  - a daily Label snapshot of every active CSP (LabelSnapshot), because the
    csp table only holds the current Label;
  - every worker job run, reading-accuracy evaluation and full document
    check (MonitorRun), because the worker's own memory is lost on restart.

Every number is defined next to the code that computes it.
"""
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .compliance import INACTIVE_LABEL, REQUIRED_TYPES, SUB_SLABS, canonical_type, evaluate, is_inactive
from .models import (AdminSession, AgreementEvent, CSP, CspQuestion, ContactChangeRequest, Document, DocumentStatus,
                     InternalUser, LabelSnapshot, ManualReviewQueue, MonitorRun, OutboundMessage, OutboundStatus,
                     ReviewStatus)

logger = logging.getLogger(__name__)
KEEP_RUNS_DAYS = 90


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db_offset(db: Session) -> timedelta:
    """Columns the database fills in itself (created_at, uploaded_at, sent_at
    of events) are in the database's local time (India); the app writes UTC.
    The difference, rounded to 15 minutes, converts the first to the second."""
    try:
        local = db.query(func.localtimestamp()).scalar()
        off = local - _now()
        return timedelta(minutes=15 * round(off.total_seconds() / 900))
    except Exception:
        return timedelta(0)


def _hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    return round((b - a).total_seconds() / 3600, 1) if a and b else None


def _status(level: str, text: str) -> dict:
    """One status line: level is ok / warn / bad; the page shows an icon and
    the text with the colour, never the colour alone."""
    return {"level": level, "text": text}


# ------------------------------------------------------------ history
def snapshot_labels(db: Session, day: Optional[date] = None) -> int:
    """Today's Label of every active CSP (replaces today's rows if run twice)."""
    day = day or date.today()
    db.query(LabelSnapshot).filter(LabelSnapshot.day == day).delete()
    n = 0
    for csp in db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)):
        st = evaluate(db, csp)
        db.add(LabelSnapshot(day=day, csp_id=csp.id, category=INACTIVE_LABEL if is_inactive(csp) else st.category, sub_slab=csp.sub_slab, rm_id=csp.rm_id,
                             docs={t: s.shown_status for t, s in st.docs.items()}))
        n += 1
    db.commit()
    return n


def has_snapshot(db: Session, day: Optional[date] = None) -> bool:
    return db.query(LabelSnapshot.id).filter(LabelSnapshot.day == (day or date.today())).first() is not None


def record_run(db: Session, kind: str, started_at: datetime, status: str, summary: Optional[dict] = None) -> None:
    """Save one run (job, evaluation, check). Never raises: monitoring must
    not break the work it watches."""
    try:
        db.add(MonitorRun(kind=kind, started_at=started_at, finished_at=_now(), status=status,
                          summary=_jsonable(summary)))
        db.query(MonitorRun).filter(MonitorRun.started_at < _now() - timedelta(days=KEEP_RUNS_DAYS)).delete()
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("monitor_run_not_saved kind=%s", kind)


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)[:500]


def _last_runs(db: Session, prefix: str, limit: int = 30) -> list[MonitorRun]:
    return (db.query(MonitorRun).filter(MonitorRun.kind.like(f"{prefix}%"))
            .order_by(MonitorRun.started_at.desc()).limit(limit).all())


def _run_json(r: Optional[MonitorRun]) -> Optional[dict]:
    if r is None:
        return None
    return {"kind": r.kind, "at": r.started_at.isoformat() + "Z", "status": r.status,     # stored in UTC
            "minutes": round((r.finished_at - r.started_at).total_seconds() / 60, 1) if r.finished_at else None,
            "summary": r.summary}


# ------------------------------------------------------------ 1. agent status
JOBS = {  # job id -> (what it is, expected gap in hours before it counts as late)
    "gmail": ("Gmail scan", 2), "sheet": ("Calling-sheet reload", 3), "engine": ("Labels + reminders (09:00)", 26),
    "outbox": ("Message sender", 0.5), "vault": ("Document folders (00:10)", 26), "reports": ("Excel reports (07:30)", 26),
    "model_assist": ("Review helper (local AI)", 26), "snapshot": ("Daily Label snapshot", 26),
    "staff_lists": ("RM / DC lists (11:00)", 26), "nudges": ("Evening nudge (17:00)", 26),
}


def agent_status(db: Session) -> dict:
    """Each job: last run, OK or failed, and whether it is late (no run for
    longer than expected). WhatsApp: messages approved today vs the daily
    limit. Gmail: last scan and backlog. Review helper: documents read today."""
    from .config import (AUTO_SEND_HOURS, MODEL_ASSIST, MODEL_ASSIST_DAILY_LIMIT, OUTBOUND_COMMUNICATION_MODE,
                         WHATSAPP_DAILY_LIMIT, local_now)
    now = _now()
    jobs = []
    for job, (label, late_h) in JOBS.items():
        runs = _last_runs(db, f"job:{job}", 50)
        last = runs[0] if runs else None
        failed_24h = sum(1 for r in runs if r.status == "FAILED" and r.started_at >= now - timedelta(hours=24))
        last_fail = next((r for r in runs if r.status == "FAILED"), None)
        if last is None:
            st = _status("warn", "no run recorded yet")
        elif last.status == "FAILED":
            st = _status("bad", "last run failed")
        elif _hours(last.started_at, now) > late_h:
            st = _status("warn", f"late: last run {_hours(last.started_at, now)} h ago")
        else:
            st = _status("ok", "running normally")
        ribbon = [{"hours_ago": _hours(r.started_at, now), "status": r.status} for r in runs
                  if r.started_at >= now - timedelta(hours=24)]
        jobs.append({"job": job, "label": label, "status": st, "last": _run_json(last), "failed_24h": failed_24h,
                     "runs_24h": ribbon,
                     "last_error": (last_fail.summary or {}).get("error") if last_fail else None})

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    wa_today = db.query(OutboundMessage).filter(
        OutboundMessage.channel == "WHATSAPP", OutboundMessage.reviewed_at >= start,
        OutboundMessage.status.in_([OutboundStatus.APPROVED, OutboundStatus.SENT, OutboundStatus.FAILED])).count()
    used = wa_today / WHATSAPP_DAILY_LIMIT if WHATSAPP_DAILY_LIMIT else 0
    hour = local_now().hour
    wa = {"today": wa_today, "limit": WHATSAPP_DAILY_LIMIT, "mode": OUTBOUND_COMMUNICATION_MODE,
          "send_hours": f"{AUTO_SEND_HOURS[0]}:00-{AUTO_SEND_HOURS[1]}:00",
          "send_window_open": AUTO_SEND_HOURS[0] <= hour < AUTO_SEND_HOURS[1],
          "status": _status("bad", "daily limit reached") if used >= 1 else
          _status("warn", f"{round(100 * used)}% of the daily limit used") if used >= 0.75 else
          _status("ok", f"{wa_today} of {WHATSAPP_DAILY_LIMIT} used today")}

    from .gmail_ingest import ingestion_status
    g = ingestion_status(db)
    gmail_last = _last_runs(db, "job:gmail", 1)
    gmail = {"last_scan": _run_json(gmail_last[0]) if gmail_last else None,
             "backfill_done": bool(g["state"].get("backfill_done") or g["state"].get("history_id")),
             "messages_by_status": g["messages_by_status"]}

    helper_runs = [r for r in _last_runs(db, "job:model_assist", 50) if r.started_at >= start]
    helper = {"on": MODEL_ASSIST, "limit": MODEL_ASSIST_DAILY_LIMIT,
              "read_today": sum(int((r.summary or {}).get("done") or 0) for r in helper_runs),
              "last": _run_json(helper_runs[0]) if helper_runs else None}

    problems = [j for j in jobs if j["status"]["level"] != "ok"] + ([wa] if wa["status"]["level"] != "ok" else [])
    overall = (_status("bad", f"{len(problems)} problem(s) need attention") if any(
        p["status"]["level"] == "bad" for p in problems) else
        _status("warn", f"{len(problems)} thing(s) to watch") if problems else _status("ok", "all running normally"))
    return {"overall": overall, "jobs": jobs, "whatsapp": wa, "gmail": gmail, "review_helper": helper}


# ------------------------------------------------------------ 2. documents in
TURN_AWAY_TEXT = {
    "wrong_doc": "not one of the three documents", "application_only": "police application receipt",
    "iibf_receipt": "IIBF fee receipt", "expired": "expired (certain)", "unreadable": "unreadable",
    "too_small": "photo too small", "too_dark": "photo too dark", "washed_out": "photo washed out",
    "blurry": "photo blurry", "no_date": "no issue date typed", "future": "date in the future",
    "too_big": "file too large", "file_type": "not a PDF/JPG/PNG", "pdf_or_photos": "PDF and photos mixed",
    "too_many_pages": "too many pages",
}


def documents_in(db: Session, days: int = 14) -> dict:
    """Documents stored per day by channel and type; what happened to them;
    why the upload page turned files away (each turned-away file once)."""
    since = _now() - timedelta(days=days) + _db_offset(db)        # in database time, like uploaded_at
    docs = db.query(Document).filter(Document.uploaded_at >= since).all()
    per_day = defaultdict(lambda: Counter())
    for d in docs:
        per_day[d.uploaded_at.date().isoformat()]["gmail" if (d.upload_channel or "").startswith("GMAIL") else "upload page"] += 1
    by_type = Counter(canonical_type(d.document_type) or "UNKNOWN" for d in docs)
    outcome = Counter(
        "unreadable" if d.readability == "UNREADABLE" else
        "in review" if d.status == DocumentStatus.NEEDS_REVIEW else
        "rejected by a person" if d.status == DocumentStatus.REJECTED else
        "duplicate" if d.status == DocumentStatus.DUPLICATE else
        "accepted" for d in docs)

    reasons, attempts, tried_nothing = Counter(), Counter(), defaultdict(int)
    for e in db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD", AgreementEvent.sent_at >= since):
        results = (e.payload or {}).get("results") or []
        attempts["submissions"] += 1
        for r in results:
            attempts["files"] += 1
            if not r.get("ok"):
                reasons[TURN_AWAY_TEXT.get(r.get("reason"), r.get("reason") or "other")] += 1
        if results and not any(r.get("ok") for r in results):
            tried_nothing[e.csp_id] += 1
    repeat = sum(1 for n in tried_nothing.values() if n >= 2)
    return {"days": days, "total": len(docs),
            "per_day": [{"day": k, **v} for k, v in sorted(per_day.items())],
            "by_type": dict(by_type), "outcome": dict(outcome),
            "upload_page": {**attempts, "turned_away_reasons": reasons.most_common(),
                            "csps_tried_twice_nothing_accepted": repeat}}


# ------------------------------------------------------------ 3. reading accuracy
def reading_accuracy(db: Session) -> dict:
    """- spot/reference checks: of auto-accepted documents a person checked,
         % read correctly (app/accuracy.py);
       - wrong rejections: upload-page files turned away that are on record
         as accepted now (re-check or review proved the reading wrong);
       - wrong acceptances: documents a person rejected after the agent
         accepted them on its own;
       - default validity: current documents whose expiry is only a default;
       - the latest reading evaluation and full document check (scripts)."""
    from . import accuracy
    acc = accuracy.stats(db, 90)
    wrong_rej, checked_rej = 0, 0
    examples = []
    for e in db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD"):
        for r in (e.payload or {}).get("results") or []:
            if r.get("ok") or r.get("reason") not in ("expired", "wrong_doc", "unreadable", "application_only",
                                                      "iibf_receipt"):
                continue
            checked_rej += 1
            did = r.get("rechecked_document_id")
            d = db.get(Document, did) if did else None
            if d is not None and d.status in (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED):
                wrong_rej += 1
                if len(examples) < 20:
                    csp = db.get(CSP, e.csp_id)
                    examples.append({"csp": csp.current_code if csp else None, "section": r.get("section"),
                                     "reason": r.get("reason"), "at": e.sent_at.isoformat() if e.sent_at else None})
    wrong_acc = (db.query(ManualReviewQueue).join(Document, Document.id == ManualReviewQueue.document_id)
                 .filter(ManualReviewQueue.status == ReviewStatus.REJECTED,
                         ManualReviewQueue.reason.like("Spot check%")).count())
    current = db.query(Document).filter(Document.is_current.is_(True),
                                        Document.status.in_([DocumentStatus.VALID, DocumentStatus.EXPIRED])).all()
    by_rule = Counter((canonical_type(d.document_type), d.validity_rule_used or "-") for d in current)
    default_risk = [d for d in current if "DEFAULT" in (d.validity_rule_used or "")]
    evals = _last_runs(db, "reading_eval", 20)
    checks = _last_runs(db, "document_check", 20)
    return {"spot_checks": acc.get("overall"), "spot_checks_by_type": acc.get("by_type"),
            "wrong_rejections": {"count": wrong_rej, "of_turned_away": checked_rej, "examples": examples},
            "wrong_acceptances": wrong_acc,
            "default_validity": {"count": len(default_risk),
                                 "by_type": dict(Counter(canonical_type(d.document_type) for d in default_risk))},
            "rules_used": [{"type": t, "rule": r, "documents": n} for (t, r), n in by_rule.most_common(25)],
            "evaluations": [_run_json(r) for r in evals], "document_checks": [_run_json(r) for r in checks]}


# ------------------------------------------------------------ 4. review queue
def _review_kind(reason: str) -> str:
    r = (reason or "").lower()
    if r.startswith("spot check"):
        return "spot check"
    if r.startswith("reference check"):
        return "reference check"
    if r.startswith("validity check"):
        return "validity only guessed"
    if r.startswith("dc upload"):
        return "DC upload (master link)"
    if "could not read" in r or "ocr could not" in r:
        return "not readable by OCR"
    if "issue date could not be read" in r:
        return "date not read"
    if "box but reads as" in r:
        return "uploaded in the wrong box"
    if "read as expired" in r or "no validity period" in r:
        return "read as expired / no validity"
    if "differs" in r or "typed" in r:
        return "typed date differs"
    if "names" in r or "could not confirm" in r:
        return "owner unclear"
    if "only from" in r or "wording" in r:
        return "type not confirmed"
    return "other"


def review_queue(db: Session) -> dict:
    """Waiting items by reason and age; decisions of the last 30 days and
    how long they took (created -> resolved); waiting items per RM."""
    now = _now()
    off = _db_offset(db)
    pending = (db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
               .join(CSP, CSP.id == Document.csp_id).filter(ManualReviewQueue.status == ReviewStatus.PENDING).all())
    ages = [max(0.0, _hours(q.created_at - off, now) or 0) for q, _, _ in pending]
    buckets = Counter("under 2 h" if a < 2 else "2-24 h" if a < 24 else "1-2 days" if a < 48 else
                      "2-7 days" if a < 168 else "over 7 days" for a in ages)
    rms = {u.id: u.name for u in db.query(InternalUser)}
    per_rm = Counter(rms.get(c.rm_id, "TBA / no RM") for _, _, c in pending)
    oldest = max(ages) if ages else None
    done = db.query(ManualReviewQueue).filter(ManualReviewQueue.resolved_at >= now - timedelta(days=30)).all()
    took = [max(0.0, _hours(q.created_at - off, q.resolved_at)) for q in done if q.created_at and q.resolved_at]
    status = (_status("ok", "nothing waiting") if not pending else
              _status("bad", f"oldest waits {round(oldest / 24, 1)} days") if oldest >= 48 else
              _status("warn", f"oldest waits {round(oldest)} h") if oldest >= 24 else
              _status("ok", f"{len(pending)} waiting, all under a day"))
    return {"status": status, "waiting": len(pending), "oldest_hours": oldest,
            "by_reason": Counter(_review_kind(q.reason) for q, _, _ in pending).most_common(),
            "by_age": [[k, buckets.get(k, 0)] for k in ("under 2 h", "2-24 h", "1-2 days", "2-7 days", "over 7 days")],
            "by_rm": per_rm.most_common(),
            "decided_30d": dict(Counter(q.status.value for q in done)),
            "median_hours_to_decision": round(median(took), 1) if took else None}


# ------------------------------------------------------------ 5. CSP experience
def csp_experience(db: Session, days: int = 7) -> dict:
    """Protecting CSPs from wrong or too many messages:
       - messages per CSP in the last `days` (sent WhatsApp/e-mail to the CSP);
       - over policy: more than one message to a CSP on the same day;
       - while waiting: messages to a CSP sent while their upload waited in review;
       - wrong 'expired/unreadable/wrong document' messages: turned-away
         uploads later accepted (see Reading accuracy);
       - failed sends (number not on WhatsApp, template error ...)."""
    since = _now() - timedelta(days=days)
    off = _db_offset(db)
    msgs = (db.query(OutboundMessage).filter(OutboundMessage.sent_at >= since,
                                             OutboundMessage.status == OutboundStatus.SENT,
                                             OutboundMessage.recipient_role.in_(["CSP", None])).all())
    per_csp = Counter(m.csp_id for m in msgs if m.csp_id)
    per_day = Counter((m.csp_id, m.sent_at.date()) for m in msgs if m.csp_id)
    over = sorted({cid for (cid, _), n in per_day.items() if n > 1})
    dist = Counter("0" if n == 0 else "1" if n == 1 else "2-3" if n <= 3 else "4-7" if n <= 7 else "8+"
                   for n in per_csp.values())
    waiting = {}
    for q, d in (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
                 .filter(ManualReviewQueue.created_at >= since - timedelta(days=30))):
        if (q.reason or "").startswith(("Spot check", "Reference check")):
            continue
        end = q.resolved_at or _now()
        waiting.setdefault(d.csp_id, []).append((q.created_at - off, end))
    while_waiting = sorted({m.csp_id for m in msgs if m.csp_id in waiting and m.template_name != "UPLOAD_RECEIPT"
                            and any(a <= m.sent_at <= b for a, b in waiting[m.csp_id])})
    failed = (db.query(OutboundMessage).filter(OutboundMessage.created_at >= since + off,
                                               OutboundMessage.status == OutboundStatus.FAILED).all())
    fail_reasons = Counter((m.error_log or "unknown")[:80] for m in failed)
    codes = {c.id: c.current_code for c in db.query(CSP.id, CSP.current_code)}
    wrong = reading_accuracy_wrong_messages(db, since + off)
    status = (_status("bad", f"{len(while_waiting)} CSP(s) messaged while their upload was being checked")
              if while_waiting else
              _status("warn", f"{len(over)} CSP(s) got more than one message on a day") if over else
              _status("ok", "no CSP over the message policy"))
    return {"days": days, "status": status, "csps_messaged": len(per_csp), "messages": len(msgs),
            "distribution": [[k, dist.get(k, 0)] for k in ("1", "2-3", "4-7", "8+")],
            "over_policy": [codes.get(c) for c in over][:50], "while_in_review": [codes.get(c) for c in while_waiting][:50],
            "wrong_negative_messages": wrong, "failed": len(failed), "failed_reasons": fail_reasons.most_common(10)}


def reading_accuracy_wrong_messages(db: Session, since: datetime) -> int:
    """Turned-away uploads (the CSP was told expired / unreadable / wrong
    document) in the period whose file is on record as accepted now."""
    n = 0
    for e in db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD", AgreementEvent.sent_at >= since):
        for r in (e.payload or {}).get("results") or []:
            if not r.get("ok") and r.get("rechecked_document_id"):
                d = db.get(Document, r["rechecked_document_id"])
                if d is not None and d.status in (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED):
                    n += 1
    return n


# ------------------------------------------------------------ 6. messaging
def messaging(db: Session, days: int = 7) -> dict:
    """Messages created in the period by status and type; waiting for
    approval and their age; how people handled the ones they reviewed."""
    now = _now()
    off = _db_offset(db)
    since = now - timedelta(days=days)
    rows = db.query(OutboundMessage).filter(OutboundMessage.created_at >= since + off).all()
    by_status = Counter(m.status.value for m in rows)
    by_type = Counter(m.template_name for m in rows)
    waiting = db.query(OutboundMessage).filter(OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW).all()
    w_ages = [max(0.0, _hours(m.created_at - off, now) or 0) for m in waiting]
    reviewed = [m for m in rows if m.reviewed_by and not (m.reviewed_by or "").startswith("agent")]
    handled = Counter("edited" if (m.payload_json or {}).get("edited") else
                      "rejected" if m.status == OutboundStatus.REJECTED else "approved as is" for m in reviewed)
    per_day = defaultdict(Counter)
    for m in rows:
        if m.sent_at:
            per_day[m.sent_at.date().isoformat()]["sent"] += 1
        if m.status == OutboundStatus.FAILED:
            per_day[(m.created_at - off).date().isoformat()]["failed"] += 1
    return {"days": days, "by_status": dict(by_status), "by_type": by_type.most_common(),
            "waiting_approval": len(waiting), "oldest_waiting_hours": max(w_ages) if w_ages else None,
            "handled_by_people": dict(handled), "per_day": [{"day": k, **v} for k, v in sorted(per_day.items())]}


# ------------------------------------------------------------ 7. network progress
def network(db: Session, days: int = 60) -> dict:
    """Label counts per day from the snapshots; today's coverage per
    document (% of active CSPs with a valid copy); renewals due."""
    since = date.today() - timedelta(days=days)
    series = defaultdict(Counter)
    for day, cat, n in (db.query(LabelSnapshot.day, LabelSnapshot.category, func.count())
                        .filter(LabelSnapshot.day >= since).group_by(LabelSnapshot.day, LabelSnapshot.category)):
        series[day.isoformat()][str(cat)] = n
    on_sheet = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).all()
    active = [c for c in on_sheet if not is_inactive(c)]          # Labels 1-4: working terminals
    labels = Counter(str(c.category) for c in active)
    labels[str(INACTIVE_LABEL)] = len(on_sheet) - len(active)
    subs = Counter(c.sub_slab for c in active if c.sub_slab)
    today = date.today()
    cover, due, expired = Counter(), Counter(), Counter()
    for c in active:
        st = evaluate(db, c)
        for t, s in st.docs.items():
            if s.status == "VALID":
                cover[t] += 1
                if s.expiry_date and s.expiry_date <= today + timedelta(days=90):
                    due[(t, 30 if s.expiry_date <= today + timedelta(days=30) else
                         60 if s.expiry_date <= today + timedelta(days=60) else 90)] += 1
            elif s.status == "EXPIRED":
                expired[t] += 1
    n = len(active) or 1
    moved = _moves(db)
    return {"series": [{"day": k, **v} for k, v in sorted(series.items())], "active_csps": len(active),
            "labels_now": dict(labels), "sub_labels_now": [[k, SUB_SLABS.get(k, k), subs[k]] for k in sorted(subs)],
            "coverage": {t: {"valid": cover[t], "percent": round(100 * cover[t] / n, 1)} for t in REQUIRED_TYPES},
            "renewals_due": {t: {"30": due[(t, 30)], "60": due[(t, 60)], "90": due[(t, 90)]} for t in REQUIRED_TYPES},
            "expired": dict(expired), "moves_7d": moved}


def _moves(db: Session, days: int = 7) -> dict:
    """CSPs whose Label went up (towards 1) or down between the snapshot of
    `days` ago (or the oldest within that window) and the latest one."""
    latest = db.query(func.max(LabelSnapshot.day)).scalar()
    if latest is None:
        return {"up": 0, "down": 0, "since": None}
    base = db.query(func.min(LabelSnapshot.day)).filter(LabelSnapshot.day >= latest - timedelta(days=days)).scalar()
    if base is None or base == latest:
        return {"up": 0, "down": 0, "since": base.isoformat() if base else None}
    old = dict(db.query(LabelSnapshot.csp_id, LabelSnapshot.category).filter(LabelSnapshot.day == base))
    up = down = 0
    for cid, cat in db.query(LabelSnapshot.csp_id, LabelSnapshot.category).filter(LabelSnapshot.day == latest):
        if cid in old and cat is not None and old[cid] is not None:
            up += cat < old[cid]
            down += cat > old[cid]
    return {"up": up, "down": down, "since": base.isoformat()}


# ------------------------------------------------------------ 8. data quality
def data_quality(db: Session) -> dict:
    """Active CSPs with no / bad / shared phone numbers, no RM, contact changes
    waiting, and current documents whose stored file is missing."""
    from . import vault
    import re
    active = db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).all()
    digits = lambda p: re.sub(r"\D", "", p or "")[-10:]
    no_phone = [c for c in active if not digits(c.whatsapp_number or c.phone)]
    bad_phone = [c for c in active if digits(c.whatsapp_number or c.phone) and
                 not re.fullmatch(r"[6-9]\d{9}", digits(c.whatsapp_number or c.phone))]
    shared = Counter(digits(c.whatsapp_number or c.phone) for c in active if digits(c.whatsapp_number or c.phone))
    shared_csps = [c for c in active if shared.get(digits(c.whatsapp_number or c.phone), 0) > 1]
    no_rm = [c for c in active if not c.rm_id]
    pending_changes = db.query(ContactChangeRequest).filter(ContactChangeRequest.status == "PENDING").count()
    missing_files = []
    for d in db.query(Document).filter(Document.is_current.is_(True)):
        p = vault.abs_path(d.storage_path) if d.storage_path else None
        if p is None or not p.exists():
            missing_files.append(d)
    codes = lambda cs: [c.current_code for c in cs][:50]
    return {"active_csps": len(active), "no_phone": codes(no_phone), "bad_phone": codes(bad_phone),
            "shared_phone": codes(shared_csps), "no_rm": len(no_rm), "contact_changes_waiting": pending_changes,
            "documents_file_missing": [{"doc": d.id, "type": d.document_type} for d in missing_files][:50],
            "counts": {"no_phone": len(no_phone), "bad_phone": len(bad_phone), "shared_phone": len(shared_csps),
                       "documents_file_missing": len(missing_files)}}


# ------------------------------------------------------------ 9. dashboard use
def dashboard_use(db: Session, days: int = 30) -> dict:
    """Totals only: sign-ins per day, and when each dashboard account last
    signed in. No pages, times or places per person."""
    since = _now() - timedelta(days=days)
    per_day = Counter(s.created_at.date().isoformat() for s in
                      db.query(AdminSession).filter(AdminSession.created_at >= since))
    users = [{"name": u.name, "role": u.role, "last_login": u.last_login_at.isoformat() if u.last_login_at else None}
             for u in db.query(InternalUser).filter(InternalUser.login_enabled.is_(True)).order_by(InternalUser.name)]
    q_open = db.query(CspQuestion).filter(CspQuestion.status == "OPEN").count()
    return {"days": days, "sign_ins_per_day": sorted(per_day.items()), "accounts": users,
            "open_csp_questions": q_open}


TABS = {"status": agent_status, "documents": documents_in, "accuracy": reading_accuracy, "review": review_queue,
        "experience": csp_experience, "messaging": messaging, "network": network, "quality": data_quality,
        "use": dashboard_use}
