"""
app/renewal_engine.py
The daily decision loop, run by the worker (app/worker.py).

For every CSP on the calling sheet:
  1. refresh its stored category (app/compliance.py);
  2. RENEWAL cycles: for each valid Agreement / PVR approaching expiry, walk
     its ladder in app/policy.py;
  3. UPLOAD cycle: while anything is missing, unreadable or expired, send an
     upload link in batches: the next reminder is drafted only once the
     previous one was SENT (or rejected) and its gap has passed, escalating
     to RM then DC;
  4. close a cycle the moment a newer valid document arrives (stop condition).

Guarantees, enforced here regardless of the policy tables:
  - only the most recent due step is drafted (a CSP who enters a ladder late
    gets one message, not a burst of old ones);
  - each step is drafted once (idempotency key "C<cycle>:<stage>");
  - at most one reminder per cycle waits for review: a newer one replaces
    an older unsent one, so drafts never pile up while nobody is sending;
  - counts below are of messages actually SENT, never of drafts;
  - the RM is never messaged before the CSP has had 2 messages in the cycle,
    and at most MAX_RM_MESSAGES times; the DC only after the RM's second
    message, and at most MAX_DC_MESSAGES times.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import ComplianceState, DOC_LABELS, refresh_category
from .models import CSP, InternalUser, OutboundMessage, OutboundStatus, OutreachCycle
from .comms.outbound import cancel_superseded, draft
from .portal_tokens import current_upload_link, issue_upload_link
from . import policy

logger = logging.getLogger(__name__)

MIN_CSP_BEFORE_RM = 2


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open_cycle(db: Session, csp_id: int, doc_type: str, kind: str) -> Optional[OutreachCycle]:
    return (db.query(OutreachCycle)
            .filter_by(csp_id=csp_id, document_type=doc_type, kind=kind, closed_at=None).first())


def _close(cycle: OutreachCycle, reason: str) -> None:
    cycle.closed_at, cycle.close_reason = _now(), reason


# Not sent yet, and may still be sent.
PENDING = (OutboundStatus.DRAFT, OutboundStatus.QUEUED_FOR_REVIEW, OutboundStatus.APPROVED,
           OutboundStatus.READY_NOT_SENT)


def _sent_stages(db: Session, cycle: OutreachCycle) -> set[str]:
    """Every stage drafted in this cycle (whatever happened to it), except
    ones the agent retired unsent: those may be drafted again, fresh."""
    return {st["stage"] for st in _stages(db, cycle)}


def _stages(db: Session, cycle: OutreachCycle) -> list[dict]:
    """The cycle's steps in the order they were drafted. A step is one stage
    (its WhatsApp + email rows): SENT once any row went out, PENDING while a
    row can still go out, otherwise DONE (rejected, blocked or failed)."""
    rows = (db.query(OutboundMessage).filter(OutboundMessage.cycle_id == cycle.id)
            .order_by(OutboundMessage.created_at, OutboundMessage.id).all())
    out: dict[str, dict] = {}
    for m in rows:
        st = out.setdefault(m.stage, {"stage": m.stage, "role": m.recipient_role, "rows": []})
        st["rows"].append(m)
    # A stage the agent retired unsent (superseded) never happened.
    out = {k: st for k, st in out.items()
           if not all(m.status == OutboundStatus.REJECTED and (m.error_log or "").startswith("Superseded")
                      for m in st["rows"])}
    for st in out.values():
        sent = [m.sent_at for m in st["rows"] if m.status == OutboundStatus.SENT and m.sent_at]
        if sent:
            st["state"], st["at"] = "SENT", min(sent)
        elif any(m.status in PENDING for m in st["rows"]):
            st["state"], st["at"] = "PENDING", None
        else:
            st["state"] = "DONE"
            st["at"] = max((m.reviewed_at or m.created_at or _now()) for m in st["rows"])
    return list(out.values())


def _sync_counts(cycle: OutreachCycle, stages: list[dict]) -> None:
    """The cycle's counters = messages actually sent, per recipient."""
    sent = [st["role"] for st in stages if st["state"] == "SENT"]
    cycle.csp_messages, cycle.rm_messages, cycle.dc_messages = sent.count("CSP"), sent.count("RM"), sent.count("DC")


def _supersede(stages: list[dict], keep: Optional[str], why: str) -> int:
    """Reject the unsent rows of every stage except `keep`, so only one
    reminder per cycle ever waits for review."""
    n = 0
    for st in stages:
        if st["stage"] == keep:
            continue
        for m in st["rows"]:
            if m.status in PENDING and m.delivery_status != "PULLED":
                m.status, m.reviewed_by, m.reviewed_at = OutboundStatus.REJECTED, "agent", _now()
                m.error_log = f"Superseded: {why}"[:500]
                n += 1
    return n


def _ctx(db: Session, csp: CSP, state: ComplianceState, link: Optional[str] = None, **extra) -> dict:
    rm = db.get(InternalUser, csp.rm_id) if csp.rm_id else None
    dc = db.get(InternalUser, csp.dc_id) if csp.dc_id else None
    docs = []
    for t, s in state.docs.items():
        en, hi = DOC_LABELS[t]
        docs.append({"type": t, "label_en": en, "label_hi": hi, "status": s.status,
                     "issue": s.issue_date.strftime("%d-%m-%Y") if s.issue_date else None,
                     "expiry": s.expiry_date.strftime("%d-%m-%Y") if s.expiry_date else None,
                     "days_left": s.days_left})
    ctx = {"csp_name": csp.name, "csp_code": csp.current_code, "csp_phone": csp.phone,
           "docs": docs, "upload_link": link,
           "rm_name": rm.name if rm else None, "rm_phone": rm.phone if rm else None,
           "dc_name": dc.name if dc else None}
    ctx.update(extra)
    return ctx


def _pick_step(cycle: OutreachCycle, due: list[dict], sent: set[str]) -> Optional[dict]:
    """The latest due step, adjusted for the escalation caps. Returns None if
    that step was already handled."""
    if not due:
        return None
    step = dict(due[-1])
    if step["stage"] in sent or f"{step['stage']}-CSP" in sent:
        return None
    if step["to"] == "RM":
        if cycle.rm_messages >= policy.MAX_RM_MESSAGES:
            return None
        if cycle.csp_messages < MIN_CSP_BEFORE_RM:
            step = {**step, "to": "CSP", "stage": f"{step['stage']}-CSP", "template": None}
    elif step["to"] == "DC":
        if cycle.dc_messages >= policy.MAX_DC_MESSAGES:
            return None
        if cycle.rm_messages < policy.MAX_RM_MESSAGES:
            step = {**step, "to": "CSP", "stage": f"{step['stage']}-CSP", "template": None}
    return step


# ------------------------------------------------------------------ renewal
def _run_renewals(db: Session, csp: CSP, state: ComplianceState, today: date) -> list[date]:
    next_dates = []
    for doc_type in ("AGREEMENT", "POLICE_VERIFICATION"):
        s = state.docs[doc_type]
        cycle = _open_cycle(db, csp.id, doc_type, "RENEWAL")
        if cycle and (s.document is None or cycle.document_id != s.document.id):
            _close(cycle, "NEWER_DOCUMENT_RECEIVED" if s.status == "VALID" else "DOCUMENT_" + s.status)
            cycle = None
        if s.status != "VALID" or s.expiry_date is None:
            continue
        key = policy.renewal_ladder_key(doc_type, s.document.validity_months)
        days_left = (s.expiry_date - today).days
        ladder = policy.LADDERS[key]
        upcoming = [st for st in ladder if st["days_before"] < days_left]
        if upcoming:
            next_dates.append(s.expiry_date - timedelta(days=upcoming[0]["days_before"]))
        due = policy.due_renewal_steps(key, days_left)
        if not due:
            continue
        if cycle is None:
            cycle = OutreachCycle(csp_id=csp.id, document_type=doc_type, kind="RENEWAL", ladder=key,
                                  anchor_date=s.expiry_date, document_id=s.document.id,
                                  csp_messages=0, rm_messages=0, dc_messages=0)
            db.add(cycle)
            db.flush()
        stages = _stages(db, cycle)
        waiting = [st for st in stages if st["state"] == "PENDING"]
        if len(waiting) > 1:     # drafted before this rule existed: keep the newest
            _supersede(stages, waiting[-1]["stage"], "only one reminder waits at a time")
            stages = _stages(db, cycle)
        _sync_counts(cycle, stages)
        step = _pick_step(cycle, due, _sent_stages(db, cycle))
        if step is None:
            continue
        # The ladder runs on the calendar (days before expiry); a newer step
        # replaces an older reminder nobody sent.
        _supersede(stages, None, f"a newer reminder ({step['stage']}) replaced it")
        en, hi = DOC_LABELS[doc_type]
        link = issue_upload_link(db, csp, [doc_type]) if step["to"] == "CSP" else None
        template = step.get("template") or "RENEWAL_FOLLOWUP"
        ctx = _ctx(db, csp, state, link, doc_label_en=en, doc_label_hi=hi,
                   expiry=s.expiry_date.strftime("%d-%m-%Y"), days_left=days_left,
                   stage=step["stage"], attempt=cycle.csp_messages)
        draft(db, csp=csp, role=step["to"], template_key=template, ctx=ctx,
              key_base=f"C{cycle.id}:{step['stage']}", cycle_id=cycle.id,
              document_type=doc_type, stage=step["stage"])
    return next_dates


# ------------------------------------------------------------------- upload
def _upload_template(state: ComplianceState) -> str:
    if state.category == 4:
        return "ONBOARD_ALL"
    if state.expired:
        # Lists every document; asks to renew the expired and upload the missing.
        return "UPLOAD_EXPIRED"
    if state.missing and all(state.docs[t].status == "UNREADABLE" for t in state.missing):
        return "UNREADABLE_REUPLOAD" if len(state.missing) == 1 else "UPLOAD_MISSING"
    return "UPLOAD_MISSING"


def _run_upload_cycle(db: Session, csp: CSP, state: ComplianceState, today: date) -> Optional[date]:
    cycle = _open_cycle(db, csp.id, "ALL", "UPLOAD")
    needed = state.needs_upload
    if not needed:
        if cycle:
            _close(cycle, "ALL_DOCUMENTS_RECEIVED")
        return None
    if cycle is None:
        cycle = OutreachCycle(csp_id=csp.id, document_type="ALL", kind="UPLOAD", ladder="UPLOAD",
                              anchor_date=today, csp_messages=0, rm_messages=0, dc_messages=0)
        db.add(cycle)
        db.flush()

    stages = _stages(db, cycle)
    sent_pos = [i for i, st in enumerate(stages) if st["state"] == "SENT"]
    if sent_pos:
        # Unsent reminders drafted before one that already went out are stale.
        stale = [st for st in stages[:sent_pos[-1]] if st["state"] == "PENDING"]
        if stale:
            _supersede(stale, None, "a later reminder was already sent")
            stages = _stages(db, cycle)
    pending = [st for st in stages if st["state"] == "PENDING"]
    if pending:
        # Waiting for the team to send it: draft nothing new. (Drafts made
        # before this rule existed: keep the oldest, retire the rest.)
        _supersede(stages, pending[0]["stage"], "only one reminder waits at a time")
        _sync_counts(cycle, stages)
        return None
    _sync_counts(cycle, stages)
    if cycle.csp_messages >= policy.MAX_CSP_UPLOAD_MESSAGES:
        return None

    # Every finished step (sent, or rejected on the dashboard) moves the
    # cycle one step on; the next waits its gap after the latest of them.
    step = dict(policy.upload_step(len(stages)))
    if stages:
        due_on = max(st["at"] for st in stages).date() + timedelta(days=step["gap"])
        if today < due_on:
            return due_on
    if step["to"] == "RM" and (cycle.rm_messages >= policy.MAX_RM_MESSAGES
                               or cycle.csp_messages < MIN_CSP_BEFORE_RM):
        step = {**step, "to": "CSP", "stage": f"{step['stage']}-CSP", "template": None}
    elif step["to"] == "DC" and (cycle.dc_messages >= policy.MAX_DC_MESSAGES
                                 or cycle.rm_messages < policy.MAX_RM_MESSAGES):
        step = {**step, "to": "CSP", "stage": f"{step['stage']}-CSP", "template": None}

    template = step.get("template") or _upload_template(state)
    single = needed[0] if len(needed) == 1 else None
    labels = DOC_LABELS.get(single, ("documents", "डॉक्यूमेंट")) if single else ("documents", "डॉक्यूमेंट")
    # Only messages to the CSP create or extend the link; an RM/DC escalation
    # just shows the link the CSP already has (so they can forward it).
    link = issue_upload_link(db, csp, needed) if step["to"] == "CSP" else current_upload_link(db, csp)
    ctx = _ctx(db, csp, state, link, stage=step["stage"], attempt=cycle.csp_messages,
               doc_label_en=labels[0], doc_label_hi=labels[1])
    draft(db, csp=csp, role=step["to"], template_key=template, ctx=ctx,
          key_base=f"C{cycle.id}:{step['stage']}", cycle_id=cycle.id,
          document_type=",".join(needed), stage=step["stage"])
    return None


# ------------------------------------------------------------ on upload
def on_documents_received(db: Session, csp: CSP, today: Optional[date] = None) -> ComplianceState:
    """Right after the portal or Gmail accepts a document: refresh the
    category, close the follow-up cycles it satisfies, and cancel their
    messages that haven't gone out yet. Nothing new is drafted here; that
    stays with the daily run."""
    today = today or date.today()
    state = refresh_category(db, csp, today)
    closed = []
    for doc_type in ("AGREEMENT", "POLICE_VERIFICATION"):
        s = state.docs[doc_type]
        cycle = _open_cycle(db, csp.id, doc_type, "RENEWAL")
        if cycle and s.status == "VALID" and s.document is not None and cycle.document_id != s.document.id:
            _close(cycle, "NEWER_DOCUMENT_RECEIVED")
            closed.append(cycle.id)
    up = _open_cycle(db, csp.id, "ALL", "UPLOAD")
    if up and not state.needs_upload:
        _close(up, "ALL_DOCUMENTS_RECEIVED")
        closed.append(up.id)
    cancel_superseded(db, csp, closed, "the CSP sent the documents")
    return state


# --------------------------------------------------------------------- run
def run_for_csp(db: Session, csp: CSP, today: Optional[date] = None) -> ComplianceState:
    today = today or date.today()
    state = refresh_category(db, csp, today)
    next_dates = _run_renewals(db, csp, state, today)
    up = _run_upload_cycle(db, csp, state, today)
    if up:
        next_dates.append(up)
    csp.next_action_at = min(next_dates) if next_dates else None
    return state


def run(db: Session, today: Optional[date] = None) -> dict:
    """One daily pass over every active CSP. Commits per CSP so one bad row
    can't block the rest."""
    today = today or date.today()
    summary = {"csps": 0, "errors": 0, "categories": {1: 0, 2: 0, 3: 0, 4: 0}}
    ids = [i for (i,) in db.query(CSP.id).filter(CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.id)]
    before = db.query(OutboundMessage).count()
    for csp_id in ids:
        try:
            csp = db.get(CSP, csp_id)
            state = run_for_csp(db, csp, today)
            db.commit()
            summary["csps"] += 1
            summary["categories"][state.category] += 1
        except Exception:
            db.rollback()
            summary["errors"] += 1
            logger.exception("renewal_engine_failed csp_id=%s", csp_id)
    summary["messages_drafted"] = db.query(OutboundMessage).count() - before
    logger.info("renewal_engine_run %s", summary)
    return summary
