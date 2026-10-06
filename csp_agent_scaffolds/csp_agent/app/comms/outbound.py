"""
app/comms/outbound.py
Drafting, the recipient guard, and sending.

Every message becomes an OutboundMessage row that is never deleted, so the
table is the permanent record of what was sent to whom, when, and why.

OUTBOUND_COMMUNICATION_MODE:
  review (testing) - drafts wait on the dashboard for Approve / Edit / Reject.
  auto   (deployment) - drafts are approved and sent by the worker.

Recipient guard: a message may only go to a phone number or email address
that the calling sheet lists for that CSP, or for its assigned RM / DC.
The destination is re-checked at send time, and the dashboard can edit a
draft's text but never its recipient.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import OUTBOUND_COMMUNICATION_MODE, WHATSAPP_DAILY_LIMIT, WHATSAPP_MODE
from ..models import CSP, InternalUser, OutboundMessage, OutboundStatus
from ..portal_tokens import extend_for_sent_message
from .templates import render
from .whatsapp import send_wabs, send_whatsapp

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
RETRY_BACKOFF_MINUTES = [5, 15, 60, 180, 720]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _phone10(v: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", v or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def _email(v: Optional[str]) -> Optional[str]:
    v = (v or "").strip().lower()
    return v if "@" in v else None


def _staff(db: Session, csp: CSP, role: str) -> Optional[InternalUser]:
    uid = csp.rm_id if role == "RM" else csp.dc_id if role == "DC" else None
    return db.get(InternalUser, uid) if uid else None


def allowed_destinations(db: Session, csp: CSP, role: str, channel: str) -> list[str]:
    """Destinations the calling sheet permits for this CSP / RM / DC."""
    if role == "CSP":
        phones = [csp.whatsapp_number, csp.phone, csp.alt_phone]
        emails = [csp.email]
    else:
        user = _staff(db, csp, role)
        phones = [user.phone] if user else []
        emails = [user.email] if user else []
    if channel == "WHATSAPP":
        return [p for p in dict.fromkeys(_phone10(x) for x in phones) if p]
    return [e for e in dict.fromkeys(_email(x) for x in emails) if e]


def recipient_allowed(db: Session, csp: CSP, role: str, channel: str, destination: str) -> bool:
    norm = _phone10(destination) if channel == "WHATSAPP" else _email(destination)
    return bool(norm) and norm in allowed_destinations(db, csp, role, channel)


def draft(db: Session, *, csp: CSP, role: str, template_key: str, ctx: dict, key_base: str,
          channels: tuple[str, ...] = ("WHATSAPP", "EMAIL"), cycle_id: Optional[int] = None,
          document_type: Optional[str] = None, stage: Optional[str] = None) -> list[OutboundMessage]:
    """Create one draft per channel. `key_base` + channel is the idempotency
    key: drafting the same step twice returns the existing rows."""
    created = []
    for channel in channels:
        key = f"{key_base}:{role}:{channel}"
        existing = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == key).first()
        if existing:
            created.append(existing)
            continue
        dests = allowed_destinations(db, csp, role, channel)
        # The link travels with the message, so the WhatsApp agent gets it as
        # its own field too (not only inside the text).
        content = {**render(template_key, channel, ctx), "link": ctx.get("upload_link")}
        msg = OutboundMessage(
            csp_id=csp.id, template_name=template_key, channel=channel,
            destination=dests[0] if dests else "-", payload_json=content,
            idempotency_key=key, cycle_id=cycle_id, document_type=document_type,
            stage=stage, recipient_role=role, attempts=0, created_at=_now(),
        )
        if not dests:
            where = "email" if channel == "EMAIL" else "WhatsApp number"
            msg.status = OutboundStatus.BLOCKED
            msg.error_log = f"No {where} for this {role} on the calling sheet."
        else:
            msg.status = (OutboundStatus.APPROVED if OUTBOUND_COMMUNICATION_MODE == "auto"
                          else OutboundStatus.QUEUED_FOR_REVIEW)
        try:
            with db.begin_nested():
                db.add(msg)
        except IntegrityError:
            msg = db.query(OutboundMessage).filter(OutboundMessage.idempotency_key == key).first()
        created.append(msg)
    return created


def approve(db: Session, msg: OutboundMessage, reviewer: str) -> OutboundMessage:
    if msg.status != OutboundStatus.QUEUED_FOR_REVIEW:
        raise ValueError(f"Only drafts awaiting review can be approved (this one is {msg.status.value}).")
    if msg.channel == "WHATSAPP" and WHATSAPP_MODE == "wabs":
        from .wabs import KIND_FOR_TEMPLATE
        if msg.template_name not in KIND_FOR_TEMPLATE:
            raise ValueError("This message type has no WhatsApp layout approved by Meta yet "
                             f"(WhatsApp can send: {', '.join(KIND_FOR_TEMPLATE)}).")
        if (msg.payload_json or {}).get("edited"):
            raise ValueError("Edited text can't go on WhatsApp: it always uses the Meta-approved layout.")
        start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        today = db.query(OutboundMessage).filter(OutboundMessage.channel == "WHATSAPP",
                                                 OutboundMessage.reviewed_at >= start,
                                                 OutboundMessage.status.in_([OutboundStatus.APPROVED, OutboundStatus.SENT,
                                                                             OutboundStatus.FAILED])).count()
        if today >= WHATSAPP_DAILY_LIMIT:
            raise ValueError(f"Daily WhatsApp limit reached ({WHATSAPP_DAILY_LIMIT} today). "
                             "Raise WHATSAPP_DAILY_LIMIT in .env to send more.")
    msg.status = OutboundStatus.APPROVED
    msg.reviewed_by, msg.reviewed_at = reviewer, _now()
    return msg


def reject(db: Session, msg: OutboundMessage, reviewer: str, reason: str) -> OutboundMessage:
    if msg.status not in (OutboundStatus.QUEUED_FOR_REVIEW, OutboundStatus.APPROVED):
        raise ValueError(f"This message can no longer be rejected ({msg.status.value}).")
    msg.status = OutboundStatus.REJECTED
    msg.reviewed_by, msg.reviewed_at = reviewer, _now()
    msg.error_log = (reason or "Rejected on the dashboard")[:500]
    return msg


def edit_text(msg: OutboundMessage, subject: Optional[str], body: Optional[str]) -> OutboundMessage:
    if msg.status != OutboundStatus.QUEUED_FOR_REVIEW:
        raise ValueError("Only drafts awaiting review can be edited.")
    payload = dict(msg.payload_json or {})
    if subject is not None:
        payload["subject"] = subject[:300]
    if body is not None:
        payload["body"] = body[:5000]
    payload["edited"] = True
    msg.payload_json = payload
    return msg


def send(db: Session, msg: OutboundMessage) -> OutboundMessage:
    """Send one APPROVED message. Re-checks the recipient guard first."""
    if msg.status != OutboundStatus.APPROVED:
        return msg
    csp = db.get(CSP, msg.csp_id) if msg.csp_id else None
    if csp is None or not recipient_allowed(db, csp, msg.recipient_role or "CSP", msg.channel, msg.destination):
        msg.status = OutboundStatus.BLOCKED
        msg.error_log = "Recipient guard: destination is not on the calling sheet for this CSP/RM/DC."
        logger.warning("outbound_blocked_by_recipient_guard message_id=%s", msg.id)
        return msg

    payload = msg.payload_json or {}
    msg.attempts = (msg.attempts or 0) + 1
    try:
        if msg.channel == "EMAIL":
            from .gmail_oauth import send_email_oauth
            resp = send_email_oauth(msg.destination, payload.get("subject", ""), payload.get("body", ""))
            msg.status, msg.provider_message_id = OutboundStatus.SENT, resp.get("id")
            msg.delivery_status, msg.sent_at, msg.error_log = "SENT", _now(), None
            extend_for_sent_message(db, payload.get("link"))
        elif msg.channel == "WHATSAPP":
            if WHATSAPP_MODE == "wabs":
                rm = _staff(db, csp, "RM")
                rm_text = (f"RM {rm.name}" + (f" ({rm.phone})" if rm.phone else "")) if rm else "आपके RM / your RM"
                out = send_wabs(msg.id, _phone10(msg.destination), msg.template_name, csp.name, csp.current_code,
                                payload.get("link"), rm_text)
            else:
                out = send_whatsapp(msg.id, _phone10(msg.destination), payload.get("body", ""),
                                    msg.idempotency_key or str(msg.id), msg.template_name, payload.get("link"))
            msg.delivery_status = out.status
            if out.status == "SENT":
                msg.status, msg.sent_at, msg.provider_message_id, msg.error_log = \
                    OutboundStatus.SENT, _now(), out.provider_message_id, None
                extend_for_sent_message(db, payload.get("link"))
            elif out.status == "READY_NOT_SENT":
                msg.status, msg.error_log = OutboundStatus.READY_NOT_SENT, "WhatsApp agent not connected yet (stub mode)."
            elif out.status == "AWAITING_PULL":
                pass  # stays APPROVED until the WhatsApp agent pulls and acks it
            else:
                _fail(msg, out.error, out.retryable)
    except Exception as e:
        _fail(msg, f"{type(e).__name__}: {e}", retryable=True)
    return msg


def _fail(msg: OutboundMessage, error: Optional[str], retryable: bool) -> None:
    msg.error_log = (error or "send failed")[:1000]
    if retryable and (msg.attempts or 0) < MAX_ATTEMPTS:
        msg.next_retry_at = _now() + timedelta(minutes=RETRY_BACKOFF_MINUTES[(msg.attempts or 1) - 1])
    else:
        msg.status = OutboundStatus.FAILED


def process_outbox(db: Session, limit: int = 50) -> dict:
    """Send approved messages whose retry time has come. Called by the worker."""
    now = _now()
    if WHATSAPP_MODE == "push":
        # Drafts approved while WhatsApp was in stub mode go out now.
        for m in db.query(OutboundMessage).filter(OutboundMessage.channel == "WHATSAPP",
                                                  OutboundMessage.status == OutboundStatus.READY_NOT_SENT):
            m.status, m.delivery_status, m.error_log = OutboundStatus.APPROVED, None, None
        db.commit()
    q = (db.query(OutboundMessage)
         .filter(OutboundMessage.status == OutboundStatus.APPROVED)
         .filter((OutboundMessage.next_retry_at.is_(None)) | (OutboundMessage.next_retry_at <= now))
         # Pull mode: the WhatsApp agent owns these (app/api/agent.py).
         .filter((OutboundMessage.delivery_status.is_(None))
                 | OutboundMessage.delivery_status.notin_(["AWAITING_PULL", "PULLED"]))
         .order_by(OutboundMessage.created_at).limit(limit))
    counts: dict[str, int] = {}
    for msg in q.all():
        send(db, msg)
        db.commit()
        counts[msg.status.value] = counts.get(msg.status.value, 0) + 1
    return counts


def cancel_superseded(db: Session, csp: CSP, cycle_ids: list[int], reason: str) -> int:
    """Messages not sent yet for follow-up cycles that just closed (the CSP
    uploaded the documents): never send them. Marked REJECTED by the agent,
    with the reason, so the history stays complete."""
    if not cycle_ids:
        return 0
    rows = db.query(OutboundMessage).filter(
        OutboundMessage.csp_id == csp.id, OutboundMessage.cycle_id.in_(cycle_ids),
        OutboundMessage.status.in_([OutboundStatus.DRAFT, OutboundStatus.QUEUED_FOR_REVIEW,
                                    OutboundStatus.APPROVED, OutboundStatus.READY_NOT_SENT])).all()
    n = 0
    for m in rows:
        if m.status == OutboundStatus.APPROVED and m.delivery_status == "PULLED":
            continue  # already handed to the WhatsApp agent
        m.status, m.reviewed_by, m.reviewed_at = OutboundStatus.REJECTED, "agent", _now()
        m.error_log = f"Cancelled before sending: {reason}"[:500]
        n += 1
    return n
