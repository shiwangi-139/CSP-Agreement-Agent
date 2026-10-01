"""
app/api/agent.py
API for the company's WhatsApp agent (X-Agent-Key). Used when
WHATSAPP_MODE=pull, and for delivery callbacks in push mode.

GET  /api/agent/outbox?limit=50        approved WhatsApp messages to send
POST /api/agent/outbox/{id}/ack        {"status": "SENT"|"FAILED", "provider_message_id", "error"}
POST /api/agent/whatsapp/status        {"message_id" | "idempotency_key", "status", ...}
GET  /api/agent/csp/{code}/status      live category, documents and dates
GET  /api/agent/csp/{code}/link        the CSP's live upload link (created if needed)

Auth: X-Agent-Key: <AGENT_API_KEY>, or Authorization: Bearer <AGENT_API_KEY>.
Only messages that already passed the recipient guard are ever listed. A
pulled message is reserved for PULL_LEASE_MINUTES; if it isn't acked by
then it is listed again, so de-duplicate on idempotency_key.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth import require_agent
from ..compliance import evaluate, CATEGORY_NAMES
from ..comms.outbound import recipient_allowed
from ..db import get_db
from ..models import CSP, OutboundMessage, OutboundStatus
from ..portal_tokens import extend_for_sent_message, issue_upload_link

router = APIRouter(dependencies=[Depends(require_agent)])
PULL_LEASE_MINUTES = 10


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/outbox")
def outbox(limit: int = 50, db: Session = Depends(get_db)):
    now = _now()
    rows = (db.query(OutboundMessage)
            .filter(OutboundMessage.channel == "WHATSAPP",
                    OutboundMessage.status.in_([OutboundStatus.APPROVED, OutboundStatus.READY_NOT_SENT]))
            .filter((OutboundMessage.delivery_status != "PULLED") | OutboundMessage.delivery_status.is_(None)
                    | (OutboundMessage.next_retry_at <= now))
            .order_by(OutboundMessage.created_at).limit(min(limit, 200)).all())
    out = []
    for m in rows:
        csp = db.get(CSP, m.csp_id)
        if csp is None or not recipient_allowed(db, csp, m.recipient_role or "CSP", "WHATSAPP", m.destination):
            m.status, m.error_log = OutboundStatus.BLOCKED, "Recipient guard: not on the calling sheet."
            continue
        m.status = OutboundStatus.APPROVED
        m.delivery_status, m.next_retry_at = "PULLED", now + timedelta(minutes=PULL_LEASE_MINUTES)
        p = m.payload_json or {}
        out.append({"id": m.id, "to": f"+91{m.destination[-10:]}", "text": p.get("body", ""),
                    "link": p.get("link"), "template_key": m.template_name, "idempotency_key": m.idempotency_key})
    db.commit()
    return {"messages": out}


def _apply_status(db: Session, m: OutboundMessage, payload: dict) -> dict:
    status = str(payload.get("status", "")).upper()
    if status not in ("SENT", "DELIVERED", "READ", "FAILED"):
        raise HTTPException(422, "status must be SENT, DELIVERED, READ or FAILED")
    m.delivery_status = status
    m.provider_message_id = payload.get("provider_message_id") or m.provider_message_id
    if status == "FAILED":
        m.status, m.error_log = OutboundStatus.FAILED, str(payload.get("error") or "Reported failed by WhatsApp agent")[:500]
    elif m.status in (OutboundStatus.APPROVED, OutboundStatus.READY_NOT_SENT):
        m.status, m.sent_at, m.next_retry_at = OutboundStatus.SENT, m.sent_at or _now(), None
        extend_for_sent_message(db, (m.payload_json or {}).get("link"))
    db.commit()
    return {"id": m.id, "status": m.status.value, "delivery_status": m.delivery_status}


@router.post("/outbox/{message_id}/ack")
def ack(message_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    m = db.get(OutboundMessage, message_id)
    if m is None or m.channel != "WHATSAPP":
        raise HTTPException(404, "message not found")
    return _apply_status(db, m, payload)


@router.post("/whatsapp/status")
def delivery_status(payload: dict = Body(...), db: Session = Depends(get_db)):
    m = None
    if payload.get("message_id"):
        m = db.get(OutboundMessage, int(payload["message_id"]))
    elif payload.get("idempotency_key"):
        m = db.query(OutboundMessage).filter_by(idempotency_key=payload["idempotency_key"]).first()
    if m is None:
        raise HTTPException(404, "message not found")
    return _apply_status(db, m, payload)


def _csp_by_code(db: Session, code: str) -> CSP:
    csp = db.query(CSP).filter((CSP.lookup_code == code.upper()) | (CSP.current_code == code.upper())).first()
    if csp is None:
        raise HTTPException(404, "CSP not found")
    return csp


@router.get("/csp/{code}/link")
def csp_link(code: str, db: Session = Depends(get_db)):
    """The CSP's live upload link, asking for whatever is still needed."""
    from ..compliance import REQUIRED_TYPES
    csp = _csp_by_code(db, code)
    st = evaluate(db, csp)
    link = issue_upload_link(db, csp, st.needs_upload or list(REQUIRED_TYPES))
    db.commit()
    return {"code": csp.current_code, "link": link, "requested": st.needs_upload or list(REQUIRED_TYPES)}


@router.get("/csp/{code}/status")
def csp_status(code: str, db: Session = Depends(get_db)):
    csp = _csp_by_code(db, code)
    st = evaluate(db, csp)
    return {"code": csp.current_code, "name": csp.name, "category": st.category,
            "category_name": CATEGORY_NAMES[st.category], "reason": st.reason,
            "missing": st.missing, "expired": st.expired,
            "documents": {t: {"status": s.status,
                              "issue_date": s.issue_date.isoformat() if s.issue_date else None,
                              "expiry_date": s.expiry_date.isoformat() if s.expiry_date else None,
                              "days_left": s.days_left} for t, s in st.docs.items()}}
