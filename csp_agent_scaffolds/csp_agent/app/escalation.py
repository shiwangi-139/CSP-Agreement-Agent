"""
app/escalation.py
The document campaign's extra messages (dates in app/policy.py), drafted by
the worker (app/worker.py) and sent in auto mode like every other message:

  evening nudge   from ESCALATE_FROM to CAMPAIGN_UNTIL, 17:00: a short
                  reminder (no link: it is in the morning message) to every
                  CSP who still has documents to upload and got the upload
                  message in the last NUDGE_AFTER_DAYS days;
  RM / DC list    11:00, one message per RM (from ESCALATE_FROM) and per DC
                  (from ESCALATE_DC_FROM): their CSPs still to upload, by KO
                  code and document. Daily during the campaign, weekly after.

The morning upload message itself is the upload cycle's daily push
(app/renewal_engine.py, policy.in_daily_push). Nothing goes to Inactive CSPs,
to a document being checked, or to DCs in policy.STAFF_LIST_SKIP. Each
message is drafted once a day (idempotency key with the date).
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import policy
from .compliance import evaluate, working_clause
from .comms import outbound
from .comms.templates import staff_list_text
from .models import CSP, InternalUser, OutboundMessage, OutboundStatus

logger = logging.getLogger(__name__)

NUDGE_AFTER_DAYS = 3          # the link must be in a message sent this recently
CODES_MAX_CHARS = 600         # WhatsApp allows about 1,000 characters in all
SHORT = {"AGREEMENT": "A", "POLICE_VERIFICATION": "P", "IIBF_CERTIFICATE": "I"}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def draft_nudges(db: Session, today: Optional[date] = None) -> dict:
    today = today or date.today()
    if not (policy.ESCALATE_FROM <= today <= policy.CAMPAIGN_UNTIL):
        return {"skipped": "not a nudge day"}
    since = _now() - timedelta(days=NUDGE_AFTER_DAYS)
    with_link = {cid for (cid,) in db.query(OutboundMessage.csp_id).filter(
        OutboundMessage.recipient_role == "CSP", OutboundMessage.channel == "WHATSAPP",
        OutboundMessage.status == OutboundStatus.SENT, OutboundMessage.sent_at >= since,
        OutboundMessage.template_name != "UPLOAD_NUDGE")}
    n = 0
    for csp in db.query(CSP).filter(working_clause(), CSP.id.in_(with_link or {-1})):
        state = evaluate(db, csp, today)
        if not state.needs_upload:
            continue
        from .compliance import DOC_LABELS
        ctx = {"csp_name": csp.name, "csp_code": csp.current_code, "docs": [],
               "doc_en": ", ".join(DOC_LABELS[t][0] for t in state.needs_upload),
               "doc_hi": ", ".join(DOC_LABELS[t][1] for t in state.needs_upload)}
        outbound.draft(db, csp=csp, role="CSP", template_key="UPLOAD_NUDGE", ctx=ctx,
                       key_base=f"NUDGE:{csp.id}:{today:%Y%m%d}", channels=("WHATSAPP",),
                       document_type=",".join(state.needs_upload), stage=f"NUDGE-{today:%Y%m%d}")
        n += 1
    db.commit()
    return {"nudges": n}


def _codes(rows: list[tuple[CSP, list[str]]]) -> str:
    """'1A851234 (P, I), 1A850973 (A)…', cut to fit one WhatsApp message."""
    out, shown = "", 0
    for csp, types in rows:
        part = f"{csp.current_code} ({', '.join(SHORT[t] for t in types)})"
        if len(out) + len(part) + 2 > CODES_MAX_CHARS:
            break
        out, shown = (f"{out}, {part}" if out else part), shown + 1
    if shown < len(rows):
        out += f" … +{len(rows) - shown} more"
    return out


def draft_staff_lists(db: Session, role: str, today: Optional[date] = None) -> dict:
    """One list per RM or DC with a phone number on record."""
    today = today or date.today()
    if not policy.staff_list_due(role, today):
        return {"skipped": f"no {role} list today"}
    field = CSP.rm_id if role == "RM" else CSP.dc_id
    pending: dict[int, list] = {}
    for csp in db.query(CSP).filter(working_clause(), field.isnot(None)).order_by(CSP.current_code):
        state = evaluate(db, csp, today)
        if state.needs_upload:
            pending.setdefault(getattr(csp, "rm_id" if role == "RM" else "dc_id"), []).append((csp, state.needs_upload))
    drafted = 0
    for uid, rows in pending.items():
        user = db.get(InternalUser, uid)
        if user is None or (user.name or "").strip().lower() in policy.STAFF_LIST_SKIP:
            continue
        phone = outbound._phone10(user.phone)
        key = f"STAFF:{role}:{uid}:{today:%Y%m%d}"
        if db.query(OutboundMessage.id).filter(OutboundMessage.idempotency_key == key).first():
            continue
        codes = _codes(rows)
        msg = OutboundMessage(
            csp_id=None, template_name="STAFF_PENDING_LIST", channel="WHATSAPP", destination=phone or "-",
            payload_json={"staff_id": uid, "subject": f"{len(rows)} CSPs still to upload ({role} {user.name})",
                          "body": staff_list_text(user.name, len(rows), codes),
                          "layout_values": {"count": len(rows), "codes": codes},
                          "csp_codes": [c.current_code for c, _ in rows]},
            idempotency_key=key, stage=f"{role}-LIST-{today:%Y%m%d}", recipient_role=role, attempts=0,
            created_at=_now(),
            status=OutboundStatus.QUEUED_FOR_REVIEW if phone else OutboundStatus.BLOCKED,
            error_log=None if phone else f"No WhatsApp number for this {role} on the calling sheet.")
        try:
            with db.begin_nested():
                db.add(msg)
            drafted += 1
        except IntegrityError:
            continue
    db.commit()
    return {f"{role.lower()}_lists": drafted}
