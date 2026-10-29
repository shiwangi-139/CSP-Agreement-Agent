"""
app/comms/send_gate.py
The last check right before a message goes out (harness), in
outbound.send(), after it was approved (by a person or by auto mode):

  paused      an admin pressed "Pause all sending": nothing goes out; the
              message waits (checked again every 15 minutes) until resumed
  quiet hours reminders and RM / DC lists wait for the morning
              (QUIET_HOURS, local time); a reply to the CSP's own upload or
              link request still goes, they have just acted
  not needed  a reminder whose follow-up cycle has closed, or that asks for
              a document the CSP no longer needs to send (it arrived, or a
              copy waits for a person): cancelled, with the reason
  repeat      the same reminder already went to the same number within
              REPEAT_HOURS: cancelled (WhatsApp refuses an exact repeat)

Waiting messages stay approved; a cancelled one is marked REJECTED by
"agent (send-time check)", so the history shows why it never went.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .. import config
from ..models import CSP, IngestState, OutboundMessage, OutboundStatus, OutreachCycle

PAUSE_KEY = "sending_paused"
PAUSE_RECHECK_MINUTES = 15
REPLIES = {"UPLOAD_RECEIPT", "UPLOAD_SUCCESS_ALL", "UPLOAD_PARTIAL_REJECTED", "LINK_REQUEST"}
NEEDS_DOCS = {"UPLOAD_MISSING", "UPLOAD_EXPIRED", "UNREADABLE_REUPLOAD", "UPLOAD_NUDGE", "ONBOARD_ALL"}
BY = "agent (send-time check)"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def pause_state(db: Session) -> dict:
    st = db.get(IngestState, PAUSE_KEY)
    v = (st.value or {}) if st is not None else {}
    return v if v.get("on") else {"on": False, **({"last": v} if v else {})}


def set_pause(db: Session, on: bool, by: str, reason: str = "") -> dict:
    st = db.get(IngestState, PAUSE_KEY)
    if st is None:
        st = IngestState(key=PAUSE_KEY, value={})
        db.add(st)
    st.value = {"on": bool(on), "by": by, "at": _now().isoformat(timespec="minutes"), "reason": (reason or "")[:300]}
    db.commit()
    return st.value


def quiet_until(now_utc: datetime) -> Optional[datetime]:
    """The end of the quiet hours (UTC) if it is quiet now, else None."""
    if not config.QUIET_HOURS:
        return None
    start, end = config.QUIET_HOURS
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(config.APP_TIMEZONE)).replace(tzinfo=None)
    h = local.hour
    quiet = (h >= start or h < end) if start > end else (start <= h < end)
    if not quiet:
        return None
    morning = local.replace(hour=end, minute=0, second=0, microsecond=0)
    if morning <= local:
        morning += timedelta(days=1)
    return now_utc + (morning - local)


def decide(db: Session, msg: OutboundMessage, now: Optional[datetime] = None) -> tuple[str, Optional[str], Optional[datetime]]:
    """("send", None, None), ("wait", reason, until) or ("cancel", reason, None)."""
    now = now or _now()
    p = pause_state(db)
    if p.get("on"):
        return "wait", f"Sending is paused by {p.get('by')}" + (f": {p['reason']}" if p.get("reason") else ""), \
            now + timedelta(minutes=PAUSE_RECHECK_MINUTES)
    if msg.template_name in REPLIES:
        return "send", None, None
    until = quiet_until(now)
    if until is not None:
        return "wait", f"Quiet hours ({config.QUIET_HOURS[0]}:00-{config.QUIET_HOURS[1]}:00): goes in the morning.", until
    if msg.cycle_id:
        cycle = db.get(OutreachCycle, msg.cycle_id)
        if cycle is not None and cycle.closed_at is not None:
            return "cancel", f"its follow-up has closed ({cycle.close_reason or 'documents received'})", None
    csp = db.get(CSP, msg.csp_id) if msg.csp_id else None
    if csp is not None and msg.template_name in NEEDS_DOCS and (msg.recipient_role or "CSP") == "CSP":
        from ..compliance import REQUIRED_TYPES, evaluate
        needed = evaluate(db, csp).needs_upload
        if not needed:
            return "cancel", "the CSP needs no document any more (received, or waiting for a person)", None
        if msg.document_type in REQUIRED_TYPES and msg.document_type not in needed:
            return "cancel", f"the CSP no longer needs to send the {msg.document_type.replace('_', ' ').lower()}", None
    repeat = (db.query(OutboundMessage.id)
              .filter(OutboundMessage.id != msg.id, OutboundMessage.template_name == msg.template_name,
                      OutboundMessage.destination == msg.destination, OutboundMessage.status == OutboundStatus.SENT,
                      OutboundMessage.sent_at >= now - timedelta(hours=config.REPEAT_HOURS))
              .order_by(OutboundMessage.sent_at.desc()).first())
    if repeat is not None:
        return "cancel", f"the same message already went to this number within {config.REPEAT_HOURS} hours (#{repeat[0]})", None
    return "send", None, None


def hold_back(db: Session, msg: OutboundMessage, now: Optional[datetime] = None) -> bool:
    """Apply the check to an approved message. True when it must not go now."""
    action, reason, until = decide(db, msg, now)
    if action == "wait":
        msg.next_retry_at, msg.error_log = until, reason[:500]
        return True
    if action == "cancel":
        msg.status, msg.reviewed_by, msg.reviewed_at = OutboundStatus.REJECTED, BY, now or _now()
        msg.error_log = f"Not sent: {reason}."[:500]
        return True
    return False


def overview(db: Session, now: Optional[datetime] = None) -> dict:
    """For the dashboard: pause state and what the check did in 7 days."""
    now = now or _now()
    since = now - timedelta(days=7)
    cancelled = (db.query(OutboundMessage).filter(OutboundMessage.reviewed_by == BY, OutboundMessage.reviewed_at >= since)
                 .order_by(OutboundMessage.reviewed_at.desc()).all())
    waiting = (db.query(OutboundMessage).filter(OutboundMessage.status == OutboundStatus.APPROVED,
                                                OutboundMessage.next_retry_at > now,
                                                OutboundMessage.error_log.like("Quiet hours%")
                                                | OutboundMessage.error_log.like("Sending is paused%")).count())
    reasons: dict[str, int] = {}
    for m in cancelled:
        r = (m.error_log or "").replace("Not sent: ", "")
        r = r.split(" (#")[0].split(" (")[0].rstrip(".")
        reasons[r] = reasons.get(r, 0) + 1
    return {"pause": pause_state(db), "quiet_hours": list(config.QUIET_HOURS or ()), "repeat_hours": config.REPEAT_HOURS,
            "cancelled_7d": len(cancelled), "cancelled_by_reason": sorted(reasons.items(), key=lambda kv: -kv[1]),
            "waiting_now": waiting,
            "examples": [{"id": m.id, "csp_id": m.csp_id, "template": m.template_name, "at": m.reviewed_at,
                          "reason": m.error_log} for m in cancelled[:8]]}
