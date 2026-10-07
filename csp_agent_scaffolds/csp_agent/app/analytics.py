"""
app/analytics.py
Messaging analytics: what was sent to CSPs, by whom, on which channel, and
what the CSPs did afterwards, per day (India time).

"Responded" is measured by what a CSP actually does, because the WhatsApp
Bulk Sender only sends and can't read replies:
  opened      the CSP opened their upload link        (AgreementEvent LINK_OPENED)
  submitted   submitted the upload page               (AgreementEvent PORTAL_UPLOAD)
  accepted    ... and at least one document accepted
  asked       asked their RM a question               (CspQuestion)
  email_reply mailed us with an attachment            (InboundMessage, from the Gmail scan)
A CSP counts for a day's messages if they did it within RESPONSE_DAYS after
the message. WhatsApp delivered / read come from Meta through the Bulk
Sender (OutboundMessage.delivery_status, refreshed by the worker).

Time: OutboundMessage times are UTC (the app's clock); event, question and
inbound times are local (the database's clock). Both become India dates here.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .config import local_now
from .models import AgreementEvent, CSP, CspQuestion, InboundMessage, OutboundMessage, OutboundStatus

IST = timedelta(hours=5, minutes=30)      # India has no daylight saving
RESPONSE_DAYS = 14
CHANNELS = ("WHATSAPP", "EMAIL")
ACTIONS = ("opened", "submitted", "accepted", "asked", "email_reply")


def _ist(utc: Optional[datetime]) -> Optional[datetime]:
    return utc + IST if utc else None


def sender_label(reviewed_by: Optional[str]) -> str:
    """Who made a message go out."""
    r = (reviewed_by or "").strip()
    if r.startswith("agent (auto"):
        return "Agent (auto mode)"
    if r.startswith("CSP asked"):
        return "Agent (CSP asked for a link)"
    if r.startswith("test_csp"):
        return "Test (test CSP)"
    return f"Approved by {r}" if r else "Unknown"


def build(db: Session, csp_ids: Optional[set[int]] = None, days: int = 14) -> dict:
    """csp_ids: limit to these CSPs (an RM's own); None = all. The test CSP
    is never counted."""
    today = local_now().date()
    first = today - timedelta(days=max(days, 30) - 1)
    start_utc = datetime.combine(first, datetime.min.time()) - IST

    test_ids = {i for (i,) in db.query(CSP.id).filter(CSP.state == "TEST")}
    keep = (lambda cid: cid not in test_ids and (csp_ids is None or cid in csp_ids))

    msgs = [m for m in db.query(OutboundMessage).filter(OutboundMessage.recipient_role == "CSP",
                                                         OutboundMessage.created_at >= start_utc)
            if keep(m.csp_id)]

    # What CSPs did, as (csp_id, local time, action).
    acts: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
    start_local = datetime.combine(first, datetime.min.time())
    for e in db.query(AgreementEvent).filter(AgreementEvent.event_type.in_(["LINK_OPENED", "PORTAL_UPLOAD"]),
                                             AgreementEvent.sent_at >= start_local):
        if e.csp_id is None or not keep(e.csp_id):
            continue
        if e.event_type == "LINK_OPENED":
            acts[e.csp_id].append((e.sent_at, "opened"))
        else:
            acts[e.csp_id].append((e.sent_at, "submitted"))
            if any(r.get("ok") for r in (e.payload or {}).get("results") or []):
                acts[e.csp_id].append((e.sent_at, "accepted"))
    for q in db.query(CspQuestion).filter(CspQuestion.created_at >= start_local):
        if keep(q.csp_id):
            acts[q.csp_id].append((q.created_at, "asked"))
    for i in db.query(InboundMessage).filter(InboundMessage.received_at >= start_local, InboundMessage.csp_id.isnot(None)):
        if keep(i.csp_id):
            acts[i.csp_id].append((i.received_at, "email_reply"))

    def did_after(csp_id: int, sent_local: datetime) -> set[str]:
        until = sent_local + timedelta(days=RESPONSE_DAYS)
        return {a for t, a in acts.get(csp_id, []) if t and sent_local <= t <= until}

    # Per India day.
    blank = lambda: {"sent": 0, "csps": set(), "failed": 0, "blocked": 0, "delivered": 0, "read": 0}
    per_day = defaultdict(lambda: {ch: blank() for ch in CHANNELS})
    responses = defaultdict(lambda: {"reached": set(), **{a: set() for a in ACTIONS}, "any": set()})
    senders = defaultdict(lambda: {ch: 0 for ch in CHANNELS})
    kinds = defaultdict(lambda: {ch: 0 for ch in CHANNELS})
    for m in msgs:
        ch = m.channel if m.channel in CHANNELS else None
        if ch is None:
            continue
        if m.status == OutboundStatus.SENT and m.sent_at:
            sent_local = _ist(m.sent_at)
            d = sent_local.date()
            row = per_day[d][ch]
            row["sent"] += 1
            row["csps"].add(m.csp_id)
            ds = (m.delivery_status or "").upper()
            if ds in ("DELIVERED", "READ"):
                row["delivered"] += 1
            if ds == "READ":
                row["read"] += 1
            r = responses[d]
            r["reached"].add(m.csp_id)
            done = did_after(m.csp_id, sent_local)
            for a in done:
                r[a].add(m.csp_id)
            if done:
                r["any"].add(m.csp_id)
            senders[sender_label(m.reviewed_by)][ch] += 1
            kinds[m.template_name or "-"][ch] += 1
        elif m.status == OutboundStatus.FAILED:
            per_day[_ist(m.reviewed_at or m.created_at).date()][ch]["failed"] += 1
        elif m.status == OutboundStatus.BLOCKED:
            per_day[_ist(m.created_at).date()][ch]["blocked"] += 1

    def day_row(d: date) -> dict:
        r = responses.get(d)
        return {"date": d.isoformat(),
                **{ch.lower(): {k: (len(v) if isinstance(v, set) else v) for k, v in per_day[d][ch].items()}
                   for ch in CHANNELS},
                "responses": {k: len(v) for k, v in r.items()} if r else
                             {"reached": 0, **{a: 0 for a in ACTIONS}, "any": 0}}

    def period(first_day: date, last_day: date) -> dict:
        out = {ch.lower(): {"sent": 0, "failed": 0, "blocked": 0, "delivered": 0, "read": 0} for ch in CHANNELS}
        reached, did = set(), {a: set() for a in (*ACTIONS, "any")}
        d = first_day
        while d <= last_day:
            for ch in CHANNELS:
                for k in out[ch.lower()]:
                    out[ch.lower()][k] += per_day[d][ch][k] if d in per_day else 0
            if d in responses:
                reached |= responses[d]["reached"]
                for a in did:
                    did[a] |= responses[d][a]
            d += timedelta(days=1)
        return {**out, "csps_reached": len(reached), **{a: len(v) for a, v in did.items()}}

    return {
        "today": today.isoformat(),
        "periods": {"today": period(today, today), "yesterday": period(today - timedelta(days=1), today - timedelta(days=1)),
                    "last_7_days": period(today - timedelta(days=6), today),
                    "last_30_days": period(today - timedelta(days=29), today)},
        "days": [day_row(today - timedelta(days=i)) for i in range(days)],
        "senders": [{"who": k, **{ch.lower(): v[ch] for ch in CHANNELS}} for k, v in
                    sorted(senders.items(), key=lambda kv: -sum(kv[1].values()))],
        "types": [{"type": k, **{ch.lower(): v[ch] for ch in CHANNELS}} for k, v in
                  sorted(kinds.items(), key=lambda kv: -sum(kv[1].values()))],
        "response_days": RESPONSE_DAYS,
    }
