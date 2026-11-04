"""
app/final_notice.py
The final notice (FINAL_NOTICE): a firm WhatsApp message to a CSP whose
documents are still pending or expired, with the deadline (6 PM today, or
tomorrow if it is drafted or sent after DEADLINE_CUTOFF_HOUR) and the CSP
common link (app/csp_common_link.py). An admin drafts it for the CSPs on
"Pending by RM" (with that page's filters); a person approves each draft in
the Communication Hub, also in auto mode.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import DOC_LABELS, ComplianceState
from .config import local_now
from .models import CSP, InternalUser

TEMPLATE = "FINAL_NOTICE"
DEADLINE_CUTOFF_HOUR = 15        # from 3 PM the deadline is 6 PM the next day


def deadline(now: Optional[datetime] = None) -> date:
    now = now or local_now()
    return now.date() + timedelta(days=1 if now.hour >= DEADLINE_CUTOFF_HOUR else 0)


def when(now: Optional[datetime] = None) -> dict:
    """"आज 04-11-2026" / "today, 04-11-2026" (or कल / tomorrow)."""
    now = now or local_now()
    d = deadline(now)
    today = d == now.date()
    return {"when_hi": f"{'आज' if today else 'कल'} {d:%d-%m-%Y}",
            "when_en": f"{'today' if today else 'tomorrow'}, {d:%d-%m-%Y}"}


def documents(state: ComplianceState) -> dict:
    """The documents still to send, one line each language; an expired one says so."""
    hi, en = [], []
    for t in state.needs_upload:
        expired = t in state.expired
        hi.append(DOC_LABELS[t][1] + (" (समाप्त)" if expired else ""))
        en.append(DOC_LABELS[t][0] + (" (expired)" if expired else ""))
    return {"docs_hi": ", ".join(hi), "docs_en": ", ".join(en)}


def rm_name(db: Session, csp: CSP) -> str:
    rm = db.get(InternalUser, csp.rm_id) if csp.rm_id else None
    return rm.name if rm is not None else ""


def context(db: Session, csp: CSP, state: ComplianceState, link: str, now: Optional[datetime] = None) -> dict:
    return {"csp_name": csp.name, "csp_code": csp.current_code, "common_link": link, "upload_link": link,
            "rm_name": rm_name(db, csp), **documents(state), **when(now)}


def draft(db: Session, csp_ids: list[int], now: Optional[datetime] = None) -> dict:
    """One WhatsApp draft per CSP that still has a document to send. Returns
    counts: drafted, already (drafted earlier for the same deadline), blocked
    (no WhatsApp number, terminal Inactive), nothing_needed."""
    from . import csp_common_link
    from .comms import outbound
    from .compliance import evaluate
    from .models import OutboundMessage, OutboundStatus
    link = csp_common_link.current_url(db)
    if link is None:
        raise ValueError("Make the CSP common link first (Form uploads & DC link).")
    now = now or local_now()
    counts = {"drafted": 0, "already": 0, "blocked": 0, "nothing_needed": 0}
    for cid in csp_ids:
        csp = db.get(CSP, cid)
        state = evaluate(db, csp) if csp is not None else None
        if state is None or not state.needs_upload:
            counts["nothing_needed"] += 1
            continue
        key_base = f"final_notice:{csp.id}:{deadline(now).isoformat()}"
        if db.query(OutboundMessage.id).filter(OutboundMessage.idempotency_key == f"{key_base}:CSP:WHATSAPP").first():
            counts["already"] += 1
            continue
        msg = outbound.draft(db, csp=csp, role="CSP", template_key=TEMPLATE, ctx=context(db, csp, state, link, now),
                             key_base=key_base, channels=("WHATSAPP",), document_type=",".join(state.needs_upload),
                             stage=TEMPLATE)[0]
        counts["blocked" if msg.status == OutboundStatus.BLOCKED else "drafted"] += 1
    return counts
