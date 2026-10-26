"""
app/insights.py
What the dashboard's "Today" page and the Agent Coach show: read-only, from
the records the agent already keeps (messages, link opens, uploads, reviews,
questions, daily Label snapshots, job runs). Nothing here changes data.

  today()      the goal (every working CSP fully compliant) and its pace,
               four headline numbers, and the CSPs that need a person today:
               who, why, and the one action to take
  journey()    the CSP journey after a reminder: messaged -> opened the link
               -> tried to upload -> a document accepted -> fully compliant,
               and how many reminders it took before CSPs uploaded
  coach()      findings about the agent itself: what is going wrong or slowly
               and what to change, most urgent first
  ask()        short answers to typed questions, from the same numbers

Times: OutboundMessage / ManualReviewQueue times are UTC (written by the app);
AgreementEvent.sent_at, Document.uploaded_at and CspQuestion.created_at are
the database's local time. _db_offset converts between them.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .compliance import DOC_LABELS, evaluate, working_clause
from .models import (AgreementEvent, CSP, CspQuestion, Document, DocumentStatus, InternalUser, LabelSnapshot,
                     ManualReviewQueue, MonitorRun, OutboundMessage, OutboundStatus, ReviewStatus)
from .monitoring import _db_offset

COMPLETE_SUBS = ("1.1", "1.2")           # all three documents valid (1.3 = one expired)
REMINDER_TEMPLATES = ("ONBOARD_ALL", "UPLOAD_MISSING", "UPLOAD_EXPIRED", "UNREADABLE_REUPLOAD", "RENEWAL_NOTICE",
                      "RENEWAL_FOLLOWUP", "RENEWAL_FINAL", "UPLOAD_NUDGE", "LINK_REQUEST")
SECTION_TYPE = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}
# Refusals that are often the agent's reading mistake, not the CSP's: a person should look.
MAYBE_WRONG = {"expired": "refused as expired", "agreement_short": "agreement refused as incomplete (1-4 pages)",
               "dup_other": "refused: same file on record for another CSP", "dup_same": "refused as the same old document",
               "dc_other_csp": "refused: reads as another CSP's document", "wrong_doc": "refused as the wrong document",
               "dup_rejected": "refused: same file rejected before"}
REFUSAL_TEXT = {**MAYBE_WRONG, "application_only": "an application receipt, not the certificate",
                "iibf_receipt": "IIBF fee receipt, not the certificate", "unreadable": "could not be read",
                "no_date": "no issue date typed", "future": "date in the future", "too_big": "file too large",
                "file_type": "not a PDF / JPG / PNG", "blurry": "blurry photo", "too_dark": "dark photo",
                "too_small": "photo too small", "washed_out": "photo washed out", "dc_valid": "already valid (locked)",
                "dc_in_review": "already being checked", "too_many_pages": "too many pages",
                "pdf_or_photos": "PDF and photos mixed"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _csp_query(db: Session, scope=None):
    q = db.query(CSP).filter(working_clause())
    return q.filter(scope) if scope is not None else q


def _brief(c: CSP) -> dict:
    return {"id": c.id, "code": c.current_code, "name": c.name}


def _ago(hours: float) -> str:
    if hours < 1:
        return "less than an hour"
    if hours < 48:
        return f"{int(hours)} h"
    return f"{int(hours // 24)} days"


# ------------------------------------------------------------------ goal
def goal(db: Session, scope=None) -> dict:
    """Every working CSP with all three documents valid: where we are, the
    daily trend from the 09:30 snapshots, and when we get there at this pace."""
    rows = _csp_query(db, scope).with_entities(CSP.category, CSP.sub_slab).all()
    total = len(rows)
    done = sum(1 for cat, sub in rows if cat == 1 and (sub in COMPLETE_SUBS or sub is None))
    since = date.today() - timedelta(days=21)
    q = db.query(LabelSnapshot.day, LabelSnapshot.category, LabelSnapshot.sub_slab).filter(
        LabelSnapshot.day >= since, LabelSnapshot.category.in_([1, 2, 3, 4]))
    if scope is not None:
        ids = [i for (i,) in _csp_query(db, scope).with_entities(CSP.id)]
        q = q.filter(LabelSnapshot.csp_id.in_(ids or [-1]))
    per_day: dict = defaultdict(lambda: [0, 0])
    for day, cat, sub in q:
        per_day[day][1] += 1
        if cat == 1 and (sub in COMPLETE_SUBS or sub is None):
            per_day[day][0] += 1
    trend = [{"day": d.isoformat(), "done": v[0], "total": v[1], "pct": round(100 * v[0] / v[1], 1) if v[1] else 0}
             for d, v in sorted(per_day.items())]
    pace = eta = None
    if len(trend) >= 3:
        last7 = trend[-8:]
        days = (date.fromisoformat(last7[-1]["day"]) - date.fromisoformat(last7[0]["day"])).days or 1
        pace = round((last7[-1]["done"] - last7[0]["done"]) / days, 1)     # CSPs completed per day
        if pace and pace > 0:
            eta = (date.today() + timedelta(days=int((total - done) / pace) + 1)).isoformat()
    return {"total": total, "done": done, "pct": round(100 * done / total, 1) if total else 0,
            "left": total - done, "trend": trend, "pace_per_day": pace, "eta": eta}


# ----------------------------------------------------------------- today
def today(db: Session, scope=None, limit: int = 60) -> dict:
    off = _db_offset(db)
    now = _now()
    local_today = datetime.combine(date.today(), datetime.min.time())
    csps = {c.id: c for c in _csp_query(db, scope)}
    ids = list(csps) or [-1]
    items: list[dict] = []
    states: dict = {}

    def needs(cid: int) -> list[str]:
        if cid not in states:
            states[cid] = evaluate(db, csps[cid])
        return states[cid].needs_upload

    # 1. Uploads refused in the last 14 days that may be the agent's mistake, while
    #    the CSP still needs that document; and CSPs who keep trying without success.
    tries: Counter = Counter()
    seen_refusal = set()
    for e in (db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD",
                                              AgreementEvent.csp_id.in_(ids),
                                              AgreementEvent.sent_at >= local_today - timedelta(days=14))
              .order_by(AgreementEvent.sent_at.desc())):
        results = (e.payload or {}).get("results") or []
        if not any(r.get("ok") for r in results):
            tries[e.csp_id] += 1
        for i, r in enumerate(results):
            t = SECTION_TYPE.get(r.get("section"))
            key = (e.csp_id, t)
            if r.get("ok") or r.get("reason") not in MAYBE_WRONG or key in seen_refusal:
                continue
            seen_refusal.add(key)
            if r.get("taken_document_id") or t not in needs(e.csp_id):
                continue
            actions = [{"label": "View what they sent", "do": "kept", "arg": f"{e.id}/{i}"}] if r.get("kept_file") else []
            if r.get("kept_file"):
                actions.append({"label": "Take this file", "do": "take", "arg": f"{e.id}/{i}", "primary": True})
            items.append({"kind": "refused", "priority": 1, "csp": _brief(csps[e.csp_id]),
                          "why": f"{DOC_LABELS[t][0]} {MAYBE_WRONG[r['reason']]} on {e.sent_at:%d-%m %H:%M}. "
                                 "Check it: if the agent read it wrong, take the file and accept it.",
                          "actions": actions + [{"label": "Open CSP", "do": "csp"}]})
    for cid, n in tries.items():
        if n >= 3 and needs(cid):
            items.append({"kind": "stuck", "priority": 1, "csp": _brief(csps[cid]),
                          "why": f"Tried to upload {n} times in 14 days, nothing accepted. A call usually solves it.",
                          "actions": [{"label": "Open CSP", "do": "csp", "primary": True},
                                      {"label": "Form uploads", "do": "view", "arg": "formuploads"}]})

    # 2. Questions waiting more than a day.
    for qn in (db.query(CspQuestion).filter(CspQuestion.status == "OPEN", CspQuestion.csp_id.in_(ids))
               .order_by(CspQuestion.created_at)):
        h = ((now + off) - qn.created_at).total_seconds() / 3600 if qn.created_at else 0
        if h >= 24:
            items.append({"kind": "question", "priority": 1 if h >= 48 else 2, "csp": _brief(csps[qn.csp_id]),
                          "why": f"Question waiting {_ago(h)}: “{(qn.text or '')[:80]}”",
                          "actions": [{"label": "Answer", "do": "view", "arg": "questions", "primary": True}]})

    # 3. Valid documents ending within 7 days (the CSP must renew now).
    soon = date.today() + timedelta(days=7)
    for d in (db.query(Document).filter(Document.csp_id.in_(ids), Document.is_current.is_(True),
                                        Document.status.in_([DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED]),
                                        Document.expiry_date >= date.today(), Document.expiry_date <= soon)
              .order_by(Document.expiry_date)):
        items.append({"kind": "expiring", "priority": 2, "csp": _brief(csps[d.csp_id]),
                      "why": f"{DOC_LABELS.get(d.document_type, (d.document_type,))[0]} ends on {d.expiry_date:%d-%m-%Y} "
                             f"({(d.expiry_date - date.today()).days} days).",
                      "actions": [{"label": "Send reminder now", "do": "remind", "primary": True},
                                  {"label": "Open CSP", "do": "csp"}]})

    # 4. Opened the link in the last 7 days but uploaded nothing since (48 h or more).
    opened = {}
    for cid, at in (db.query(AgreementEvent.csp_id, func.max(AgreementEvent.sent_at))
                    .filter(AgreementEvent.event_type == "LINK_OPENED", AgreementEvent.csp_id.in_(ids),
                            AgreementEvent.sent_at >= local_today - timedelta(days=7))
                    .group_by(AgreementEvent.csp_id)):
        opened[cid] = at
    if opened:
        uploaded = dict(db.query(AgreementEvent.csp_id, func.max(AgreementEvent.sent_at))
                        .filter(AgreementEvent.event_type == "PORTAL_UPLOAD", AgreementEvent.csp_id.in_(list(opened)))
                        .group_by(AgreementEvent.csp_id).all())
        for cid, at in opened.items():
            h = ((now + off) - at).total_seconds() / 3600
            if h >= 48 and (uploaded.get(cid) is None or uploaded[cid] < at) and needs(cid):
                items.append({"kind": "opened_no_upload", "priority": 2, "csp": _brief(csps[cid]),
                              "why": f"Opened the upload link {_ago(h)} ago but uploaded nothing. They may be stuck: call, or nudge.",
                              "actions": [{"label": "Send reminder now", "do": "remind", "primary": True},
                                          {"label": "Open CSP", "do": "csp"}]})

    # 5. Their latest WhatsApp failed (nothing reached them since).
    last = {}
    for m in (db.query(OutboundMessage).filter(OutboundMessage.csp_id.in_(ids), OutboundMessage.channel == "WHATSAPP",
                                               OutboundMessage.recipient_role == "CSP",
                                               OutboundMessage.created_at >= now - timedelta(days=3),
                                               OutboundMessage.status.in_([OutboundStatus.SENT, OutboundStatus.FAILED]))
              .order_by(OutboundMessage.created_at)):
        last[m.csp_id] = m
    for cid, m in last.items():
        if m.status == OutboundStatus.FAILED and needs(cid):
            items.append({"kind": "failed", "priority": 2, "csp": _brief(csps[cid]),
                          "why": f"Last WhatsApp ({m.template_name.replace('_', ' ').lower()}) failed: {(m.error_log or '')[:90]}",
                          "actions": [{"label": "Send reminder now", "do": "remind", "primary": True},
                                      {"label": "Open CSP", "do": "csp"}]})

    # 6. The Review queue, as one item.
    pend = (db.query(ManualReviewQueue).join(Document, Document.id == ManualReviewQueue.document_id)
            .filter(ManualReviewQueue.status == ReviewStatus.PENDING, Document.csp_id.in_(ids)))
    n_review = pend.count()
    oldest = pend.order_by(ManualReviewQueue.created_at).first()
    if n_review:
        h = (now - oldest.created_at).total_seconds() / 3600 if oldest and oldest.created_at else 0
        items.append({"kind": "review", "priority": 1 if h >= 48 else 2, "csp": None,
                      "why": f"{n_review} document(s) waiting in the Review queue, the oldest for {_ago(h)}. "
                             "Until you decide, those CSPs get no reminder for them.",
                      "actions": [{"label": "Open Review queue", "do": "view", "arg": "review", "primary": True}]})

    items.sort(key=lambda x: (x["priority"], x["kind"] != "refused", (x["csp"] or {}).get("code", "")))
    accepted_today = (db.query(func.count(Document.id)).filter(
        Document.csp_id.in_(ids), Document.uploaded_at >= local_today,
        Document.status.in_([DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED, DocumentStatus.EXPIRED])).scalar())
    failed_today = (db.query(func.count(func.distinct(OutboundMessage.csp_id))).filter(
        OutboundMessage.csp_id.in_(ids), OutboundMessage.channel == "WHATSAPP",
        OutboundMessage.status == OutboundStatus.FAILED,
        OutboundMessage.created_at >= local_today - off).scalar())
    sent_today = (db.query(func.count(OutboundMessage.id)).filter(
        OutboundMessage.csp_id.in_(ids), OutboundMessage.channel == "WHATSAPP",
        OutboundMessage.status == OutboundStatus.SENT, OutboundMessage.sent_at >= local_today - off).scalar())
    return {"goal": goal(db, scope), "attention": items[:limit], "attention_total": len(items),
            "tiles": {"accepted_today": accepted_today, "review_waiting": n_review,
                      "whatsapp_sent_today": sent_today, "whatsapp_failed_today": failed_today}}


# --------------------------------------------------------------- journey
def journey(db: Session, scope=None, days: int = 14) -> dict:
    """After the first reminder in the window: how many CSPs opened their link,
    tried to upload, got a document accepted, and are now fully compliant; and
    how many reminders it took before they first uploaded."""
    off = _db_offset(db)
    since = _now() - timedelta(days=days)
    csps = {c.id: c for c in _csp_query(db, scope)}
    ids = list(csps) or [-1]
    first: dict = {}
    sent_times: dict = defaultdict(list)
    for cid, at in (db.query(OutboundMessage.csp_id, OutboundMessage.sent_at)
                    .filter(OutboundMessage.csp_id.in_(ids), OutboundMessage.channel == "WHATSAPP",
                            OutboundMessage.recipient_role == "CSP", OutboundMessage.status == OutboundStatus.SENT,
                            OutboundMessage.template_name.in_(REMINDER_TEMPLATES),
                            OutboundMessage.sent_at >= since - timedelta(days=60))
                    .order_by(OutboundMessage.sent_at)):
        sent_times[cid].append(at)
        if at >= since and cid not in first:
            first[cid] = at
    events: dict = defaultdict(list)                 # csp -> [(utc time, type, payload)]
    for cid, kind, at, payload in (db.query(AgreementEvent.csp_id, AgreementEvent.event_type, AgreementEvent.sent_at,
                                            AgreementEvent.payload)
                                   .filter(AgreementEvent.csp_id.in_(ids),
                                           AgreementEvent.event_type.in_(["LINK_OPENED", "PORTAL_UPLOAD"]),
                                           AgreementEvent.sent_at >= since + off - timedelta(days=1))):
        events[cid].append((at - off, kind, payload or {}))
    opened = tried = accepted = 0
    for cid, t0 in first.items():
        ev = [e for e in events.get(cid, []) if e[0] >= t0]
        opened += any(k == "LINK_OPENED" for _, k, _ in ev) or any(k == "PORTAL_UPLOAD" for _, k, _ in ev)
        ups = [p for _, k, p in ev if k == "PORTAL_UPLOAD"]
        tried += bool(ups)
        accepted += any(r.get("ok") for p in ups for r in (p.get("results") or []))
    complete = sum(1 for cid in first if csps[cid].category == 1 and (csps[cid].sub_slab in COMPLETE_SUBS or csps[cid].sub_slab is None))
    tracking_since = db.query(func.min(AgreementEvent.sent_at)).filter(AgreementEvent.event_type == "LINK_OPENED").scalar()
    steps = [{"step": "Got a reminder", "n": len(first)}, {"step": "Opened their link", "n": opened},
             {"step": "Tried to upload", "n": tried}, {"step": "A document accepted", "n": accepted},
             {"step": "Fully compliant now", "n": complete}]
    # Reminders before the first upload (CSPs whose first upload is in the window).
    hist = Counter()
    waits = []
    for cid, ev in events.items():
        ups = sorted(t for t, k, _ in ev if k == "PORTAL_UPLOAD" and t >= since)
        if not ups:
            continue
        before = [s for s in sent_times.get(cid, []) if s <= ups[0] and s >= ups[0] - timedelta(days=60)]
        hist[min(len(before), 6)] += 1
        if before:
            waits.append((ups[0] - before[-1]).total_seconds() / 3600)
    buckets = [{"label": "0 (by themselves)" if k == 0 else ("6+" if k == 6 else str(k)), "n": hist.get(k, 0)}
               for k in range(7)]
    return {"days": days, "steps": steps,
            "link_tracking_since": tracking_since.isoformat() if tracking_since else None,
            "reminders_before_upload": buckets,
            "median_hours_last_reminder_to_upload": round(median(waits), 1) if waits else None}


# ----------------------------------------------------------------- coach
def coach(db: Session) -> list[dict]:
    """What to improve, most urgent first: level critical / warn / info / good,
    a title, the numbers behind it, and the one thing to do."""
    out: list[dict] = []
    add = lambda level, title, detail, action=None: out.append(
        {"level": level, "title": title, "detail": detail, "action": action})
    now = _now()
    off = _db_offset(db)
    local_today = datetime.combine(date.today(), datetime.min.time())

    # The agent's own jobs.
    def last_ok(kind):
        return (db.query(func.max(MonitorRun.started_at)).filter(MonitorRun.kind == kind,
                                                                 MonitorRun.status.in_(["OK", "SKIPPED"])).scalar())
    outbox = last_ok("job:outbox")
    if outbox is None or now - outbox > timedelta(minutes=15):
        add("critical", "The worker is not running",
            f"The message sender last ran {('at ' + (outbox + off).strftime('%d-%m %H:%M')) if outbox else 'never'}: "
            "no reminders or replies are going out.",
            {"label": "Check the server (scripts/restart.sh)", "do": "view", "arg": "monitoring"})
    engine = last_ok("job:engine")
    if datetime.now().hour >= 10 and (engine is None or engine + off < local_today + timedelta(hours=8)):
        add("critical", "Today's 09:00 run did not happen",
            "Labels and today's reminders were not worked out today.",
            {"label": "Run labels & follow-ups now", "do": "run", "arg": "engine"})
    sheet = (db.query(MonitorRun).filter(MonitorRun.kind == "job:sheet").order_by(MonitorRun.started_at.desc()).first())
    if sheet and "saved copy" in str((sheet.summary or {}).get("source", "")):
        add("warn", "The calling sheet is an old saved copy",
            "New CSPs, numbers and Terminal Status changes on the live sheet don't reach the agent.",
            {"label": "Fix CALLING_SHEET_LINK in .env", "do": "view", "arg": "monitoring"})

    # WhatsApp: layouts failing, the daily limit.
    fails = Counter()
    for tpl, err in (db.query(OutboundMessage.template_name, OutboundMessage.error_log)
                     .filter(OutboundMessage.channel == "WHATSAPP", OutboundMessage.status == OutboundStatus.FAILED,
                             OutboundMessage.created_at >= now - timedelta(days=2))):
        fails[(tpl, (err or "")[:70])] += 1
    for (tpl, err), n in fails.most_common(3):
        if n >= 5:
            stuck = "Hindi content" in err or "template" in err.lower()
            add("critical" if n >= 20 else "warn", f"{n} WhatsApp '{tpl.replace('_', ' ').lower()}' failed in 2 days",
                f"Reason: {err}…" + (" The layout is stuck at Meta: give it a new template name." if stuck else ""),
                {"label": "See failed messages", "do": "view", "arg": "comms"})
    runs = (db.query(MonitorRun.summary).filter(MonitorRun.kind == "job:outbox",
                                                MonitorRun.started_at >= local_today - off).all())
    if any("limit_reached" in str(s or "") for (s,) in runs):
        add("critical", "The daily WhatsApp limit was reached today",
            "Messages beyond it wait until tomorrow. Raise WHATSAPP_DAILY_LIMIT in .env (within what the Bulk "
            "Sender allows) and restart.", {"label": "See waiting messages", "do": "view", "arg": "comms"})

    # Refusals of the last 7 days: what turns CSPs away.
    reasons = Counter()
    total_up = 0
    for (payload,) in (db.query(AgreementEvent.payload).filter(AgreementEvent.event_type == "PORTAL_UPLOAD",
                                                              AgreementEvent.sent_at >= local_today - timedelta(days=7))):
        for r in (payload or {}).get("results") or []:
            total_up += 1
            if not r.get("ok"):
                reasons[r.get("reason") or "?"] += 1
    refused = sum(reasons.values())
    if total_up:
        top = ", ".join(f"{REFUSAL_TEXT.get(k, k)} {v}" for k, v in reasons.most_common(4))
        share = round(100 * refused / total_up)
        maybe = sum(v for k, v in reasons.items() if k in MAYBE_WRONG)
        level = "warn" if share >= 35 else "info"
        add(level, f"{share}% of documents sent in 7 days were refused ({refused} of {total_up})",
            f"Most common: {top}." + (f" {maybe} of them are refusals that are often the agent's reading mistake: "
                                      "check them on Today and take the file if it was right." if maybe else ""),
            {"label": "See refused uploads", "do": "view", "arg": "formuploads"})

    # Review backlog and questions.
    pend = db.query(ManualReviewQueue).filter(ManualReviewQueue.status == ReviewStatus.PENDING)
    n_rev = pend.count()
    oldest = pend.order_by(ManualReviewQueue.created_at).first()
    if n_rev:
        h = (now - oldest.created_at).total_seconds() / 3600 if oldest and oldest.created_at else 0
        add("warn" if h >= 48 or n_rev >= 20 else "info", f"{n_rev} documents wait in the Review queue",
            f"The oldest has waited {_ago(h)}. Those CSPs get no reminder for that document until you decide.",
            {"label": "Open Review queue", "do": "view", "arg": "review"})
    orphan = (db.query(func.count(Document.id)).filter(
        Document.status.in_([DocumentStatus.NEEDS_REVIEW, DocumentStatus.NEEDS_APPROVAL]),
        ~Document.id.in_(db.query(ManualReviewQueue.document_id).filter(ManualReviewQueue.status == ReviewStatus.PENDING)))
        .scalar())
    if orphan:
        add("warn", f"{orphan} documents wait for a person but are not in the Review queue",
            "Nobody can check them. Run scripts/queue_waiting_documents.py (dry run, then --apply) on the server.")
    q_old = (db.query(func.count(CspQuestion.id)).filter(CspQuestion.status == "OPEN",
                                                         CspQuestion.created_at <= now + off - timedelta(hours=48)).scalar())
    if q_old:
        add("warn", f"{q_old} CSP questions unanswered for 2+ days",
            "A CSP who asks and gets no answer often stops trying.", {"label": "Answer questions", "do": "view", "arg": "questions"})

    # The journey: where CSPs drop off, and whether more reminders help.
    j = journey(db, None, 14)
    s = {x["step"]: x["n"] for x in j["steps"]}
    got, op, tr, acc = s["Got a reminder"], s["Opened their link"], s["Tried to upload"], s["A document accepted"]
    if got >= 20:
        if j["link_tracking_since"] and op / got < 0.35:
            add("warn", f"Only {round(100 * op / got)}% of reminded CSPs opened their link",
                "The message may not reach them (wrong number, not read) or not convince them. Try calls for the "
                "rest, and check the numbers on the calling sheet.", {"label": "See message reach", "do": "view", "arg": "comms"})
        if op and tr / op < 0.5:
            add("warn", f"{round(100 * (1 - tr / op))}% opened the link but never tried to upload",
                "Something on the upload page stops them (they don't have the document yet, or don't know how). "
                "Call a few and ask.", None)
        if tr and acc / tr < 0.6:
            add("warn", f"Only {round(100 * acc / tr)}% of CSPs who uploaded got a document accepted",
                "Most refusals are fixable: see the refusal reasons above.", {"label": "See refused uploads", "do": "view", "arg": "formuploads"})
    h = {b["label"]: b["n"] for b in j["reminders_before_upload"]}
    early = sum(h.get(k, 0) for k in ("0 (by themselves)", "1", "2", "3"))
    late = sum(h.get(k, 0) for k in ("4", "5", "6+"))
    if early + late >= 15:
        add("info", f"{round(100 * early / (early + late))}% of uploads came within 3 reminders",
            "After the 3rd reminder few more CSPs upload by themselves: from then on a call from the RM or DC works "
            "better than more WhatsApp." if late / (early + late) < 0.2 else
            "Many CSPs need 4+ reminders: keep the daily campaign going.", None)
    if not j["link_tracking_since"]:
        add("info", "Link opens are now being recorded",
            "From now on the Coach can tell who opened their link but didn't upload.", None)

    # The goal and its pace.
    g = goal(db)
    if g["pace_per_day"] is not None:
        if g["pace_per_day"] <= 0:
            add("warn", "No progress towards the goal this week",
                f"{g['done']} of {g['total']} working CSPs are fully compliant ({g['pct']}%), no better than a week ago.", None)
        else:
            add("good", f"{g['pct']}% of working CSPs are fully compliant",
                f"{g['pace_per_day']} more CSPs a day this week; at this pace all {g['total']} by "
                f"{date.fromisoformat(g['eta']):%d-%m-%Y}." if g["eta"] else "", None)
    order = {"critical": 0, "warn": 1, "info": 2, "good": 3}
    out.sort(key=lambda f: order[f["level"]])
    return out


# ------------------------------------------------------------------- ask
QUESTIONS = ["Who should I call today?", "Why are uploads refused?", "How are we doing on the goal?",
             "Are WhatsApp messages going out?", "Do more reminders help?", "Which DC is furthest behind?"]


def ask(db: Session, question: str, scope=None) -> dict:
    """A short answer from the same numbers (no AI model: nothing is guessed)."""
    q = (question or "").lower()
    if any(w in q for w in ("call", "today", "attention", "who")):
        t = today(db, scope, limit=8)
        lines = [f"{i['csp']['code']} {i['csp']['name']}: {i['why']}" if i["csp"] else i["why"] for i in t["attention"]]
        return {"answer": f"{t['attention_total']} things need a person today. Start with:", "lines": lines,
                "action": {"label": "Open Today", "do": "view", "arg": "today"}}
    if any(w in q for w in ("refus", "reject", "turned away", "why")):
        f = next((x for x in coach(db) if "refused" in x["title"]), None)
        return {"answer": f["title"] if f else "No uploads in the last 7 days.", "lines": [f["detail"]] if f else [],
                "action": f and f["action"]}
    if any(w in q for w in ("goal", "progress", "doing", "complian")):
        g = goal(db, scope)
        lines = [f"{g['done']} of {g['total']} working CSPs fully compliant ({g['pct']}%), {g['left']} to go."]
        if g["pace_per_day"] is not None:
            lines.append(f"Pace this week: {g['pace_per_day']} CSPs a day"
                         + (f"; all done by {date.fromisoformat(g['eta']):%d-%m-%Y} at this pace." if g["eta"] else "."))
        return {"answer": "The goal: every working CSP with all three documents valid.", "lines": lines}
    if any(w in q for w in ("whatsapp", "message", "sent", "fail", "template")):
        msgs = [f for f in coach(db) if "WhatsApp" in f["title"] or "worker" in f["title"]]
        t = today(db, scope, limit=1)["tiles"]
        return {"answer": f"Today: {t['whatsapp_sent_today']} WhatsApp sent, {t['whatsapp_failed_today']} CSPs whose message failed.",
                "lines": [f"{m['title']}. {m['detail']}" for m in msgs] or ["No repeated failures in the last 2 days."],
                "action": {"label": "Open Communication Hub", "do": "view", "arg": "comms"}}
    if any(w in q for w in ("remind", "follow", "nudge", "how many")):
        j = journey(db, scope)
        lines = [f"{b['label']} reminders before uploading: {b['n']} CSPs" for b in j["reminders_before_upload"] if b["n"]]
        if j["median_hours_last_reminder_to_upload"] is not None:
            lines.append(f"Typically {j['median_hours_last_reminder_to_upload']} h from the last reminder to the upload.")
        return {"answer": f"CSPs who uploaded in the last {j['days']} days, by reminders received before:", "lines": lines}
    if "dc" in q or "behind" in q:
        from .reports import dc_progress
        rows = sorted(dc_progress(db)["rows"], key=lambda r: -r["left_now"])
        return {"answer": "DCs by CSPs still to upload:",
                "lines": [f"{r['dc']}: {r['left_now']} of {r['csps']} left, all 3 OK {r['complete_now']}" for r in rows[:8]]}
    return {"answer": "I can answer from the agent's own numbers. Try one of these:", "lines": QUESTIONS}
