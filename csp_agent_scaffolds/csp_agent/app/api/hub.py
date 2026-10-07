"""
app/api/hub.py
Data for the dashboard (app/web/dashboard.html). All routes need X-API-Key.
"""
import io
import logging
import threading
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import Principal, require_admin, require_user
from ..compliance import (CATEGORY_NAMES, DOC_LABELS, REQUIRED_TYPES, SUB_SLABS, canonical_type, evaluate,
                          sub_slab, sub_slab_label)
from ..comms import outbound
from ..config import (OUTBOUND_COMMUNICATION_MODE, WHATSAPP_MODE, CALLING_SHEET_LINK, CALLING_SHEET_SOURCE,
                      CALLING_SHEET_TAB)
from ..db import get_db, SessionLocal
from ..models import (CSP, ContactChangeRequest, Document, DocumentStatus, ExtractionCorrection, InboundMessage, InternalUser,
                      ManualReviewQueue, OutboundMessage, OutboundStatus, OutreachCycle, ReviewStatus)
from ..portal_tokens import issue_upload_link
from .. import vault
from ..vault import csp_folder

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_user)])

_jobs: dict[str, dict] = {}


def _mine(query, me: Principal):
    """An RM sees only their own CSPs; an admin sees all. `query` must
    involve the CSP table."""
    return query if me.is_admin else query.filter(CSP.rm_id == me.user_id)


def _own_csp(db: Session, csp_id: Optional[int], me: Principal) -> CSP:
    c = db.get(CSP, csp_id) if csp_id else None
    if c is None or not (me.is_admin or c.rm_id == me.user_id):
        raise HTTPException(404, "CSP not found")      # never confirm another RM's CSP exists
    return c


def _iso(v):
    return v.isoformat() if v else None


def _staff_map(db: Session) -> dict[int, InternalUser]:
    return {u.id: u for u in db.query(InternalUser).all()}


def _doc_json(d: Document) -> dict:
    f = d.extracted_fields or {}
    return {"id": d.id, "type": canonical_type(d.document_type), "status": d.status.value,
            "readability": d.readability, "issue_date": _iso(d.issue_date), "expiry_date": _iso(d.expiry_date),
            "days_left": (d.expiry_date - date.today()).days if d.expiry_date else None,
            "validity_rule": d.validity_rule_used, "date_source": d.date_source,
            "method": d.extraction_method, "model": f.get("model_provider"), "is_current": d.is_current,
            "channel": d.upload_channel, "filename": d.original_filename, "uploaded_at": _iso(d.uploaded_at),
            "source_date": _iso(d.source_date), "sender_on_sheet": d.sender_on_sheet,
            "holder_name": d.holder_name, "registration_number": d.iibf_reg_number,
            "typed_issue_date": f.get("typed_issue_date"),
            "reason": f.get("readability_reason") or f.get("rejection_reason")}


def _msg_json(m: OutboundMessage, csp: Optional[CSP]) -> dict:
    p = m.payload_json or {}
    return {"id": m.id, "csp_id": m.csp_id, "csp_code": csp.current_code if csp else None,
            "csp_name": csp.name if csp else None, "template": m.template_name, "channel": m.channel,
            "to": m.destination, "role": m.recipient_role, "stage": m.stage, "document_type": m.document_type,
            "subject": p.get("subject"), "body": p.get("body"), "edited": bool(p.get("edited")),
            "status": m.status.value, "delivery_status": m.delivery_status, "error": m.error_log,
            "created_at": _iso(m.created_at), "sent_at": _iso(m.sent_at),
            "reviewed_by": m.reviewed_by, "reviewed_at": _iso(m.reviewed_at)}


# ------------------------------------------------------------------ summary
@router.get("/summary")
def summary(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    active = _mine(db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)), me)
    cats = dict(active.with_entities(CSP.category, func.count()).group_by(CSP.category).all())
    # Within each slab: CSPs with at least one expired document (renewal due).
    expired_in = dict(active.filter(CSP.category_reason.ilike("%Expired:%"))
                      .with_entities(CSP.category, func.count()).group_by(CSP.category).all())
    subs = dict(active.with_entities(CSP.sub_slab, func.count()).group_by(CSP.sub_slab).all())
    msg_counts = dict(_mine(db.query(OutboundMessage.channel, func.count())
                            .join(CSP, CSP.id == OutboundMessage.csp_id), me)
                      .filter(OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW)
                      .group_by(OutboundMessage.channel).all())
    gaps = {"csp_phone": 0, "csp_email": 0, "rm_unassigned": 0, "dc_unassigned": 0}
    for (g,) in active.with_entities(CSP.contact_gaps):
        g = g or {}
        gaps["csp_phone"] += "phone_missing" in g.get("csp", [])
        gaps["csp_email"] += "email_missing" in g.get("csp", [])
        gaps["rm_unassigned"] += "not_assigned" in g.get("rm", [])
        gaps["dc_unassigned"] += "not_assigned" in g.get("dc", [])
    soon = (_mine(db.query(func.count(Document.id)).join(CSP, CSP.id == Document.csp_id), me)
            .filter(Document.is_current.is_(True), Document.expiry_date.isnot(None),
                    Document.expiry_date >= date.today(),
                    Document.expiry_date <= date.fromordinal(date.today().toordinal() + 60)).scalar())
    from ..gmail_ingest import ingestion_status
    return {
        "total_csps": active.count(),
        "categories": {str(k): cats.get(k, 0) for k in (1, 2, 3, 4)},
        "categories_with_expired": {str(k): expired_in.get(k, 0) for k in (1, 2, 3, 4)},
        "sub_slabs": {code: {"label": label, "count": subs.get(code, 0)} for code, label in SUB_SLABS.items()},
        "tags": {"expired": sum(expired_in.values()),
                 "unreachable": active.filter(or_(CSP.phone.is_(None), CSP.phone == ""),
                                              or_(CSP.whatsapp_number.is_(None), CSP.whatsapp_number == ""),
                                              or_(CSP.email.is_(None), CSP.email == "")).count(),
                 "no_rm": active.filter(CSP.rm_id.is_(None)).count()},
        "uncategorised": cats.get(None, 0),
        "drafts_pending": {"EMAIL": msg_counts.get("EMAIL", 0), "WHATSAPP": msg_counts.get("WHATSAPP", 0)},
        "expiring_60_days": soon,
        "contact_gaps": gaps,
        "review_pending": _mine(db.query(ManualReviewQueue).join(Document, Document.id == ManualReviewQueue.document_id)
                                .join(CSP, CSP.id == Document.csp_id), me)
                          .filter(ManualReviewQueue.status == ReviewStatus.PENDING).count(),
        "contact_changes_pending": db.query(ContactChangeRequest).filter_by(status="PENDING").count() if me.is_admin else 0,
        "me": {"name": me.name, "role": me.role},
        "modes": {"outbound": OUTBOUND_COMMUNICATION_MODE, "whatsapp": WHATSAPP_MODE,
                  "calling_sheet": (f"live sheet link · {CALLING_SHEET_TAB}" if CALLING_SHEET_LINK
                                    else f"{CALLING_SHEET_SOURCE} · {CALLING_SHEET_TAB}")},
        "ingestion": ingestion_status(db),
        "jobs": _jobs if me.is_admin else {},
    }


# --------------------------------------------------------------------- CSPs
@router.get("/csps")
def list_csps(category: Optional[int] = None, q: str = "", rm: str = "", expired: bool = False,
              sub: str = "", tag: str = "", page: int = 1, size: int = 50,
              me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    query = _mine(db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)), me)
    if category:
        query = query.filter(CSP.category == category)
    if expired or tag == "expired":
        query = query.filter(CSP.category_reason.ilike("%Expired:%"))
    if sub in SUB_SLABS:
        query = query.filter(CSP.sub_slab == sub)
    if tag == "unreachable":
        query = query.filter(or_(CSP.phone.is_(None), CSP.phone == ""),
                             or_(CSP.whatsapp_number.is_(None), CSP.whatsapp_number == ""),
                             or_(CSP.email.is_(None), CSP.email == ""))
    if tag == "no_rm":
        query = query.filter(CSP.rm_id.is_(None))
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(CSP.current_code.ilike(like), CSP.name.ilike(like), CSP.phone.ilike(like)))
    staff = _staff_map(db)
    if rm:
        ids = [u.id for u in staff.values() if u.role == "RM" and u.name.lower() == rm.lower()]
        query = query.filter(CSP.rm_id.in_(ids or [-1]))
    total = query.count()
    rows = query.order_by(CSP.next_action_at.asc().nullslast(), CSP.current_code).offset((page - 1) * size).limit(min(size, 200)).all()
    out = []
    for c in rows:
        st = evaluate(db, c)
        out.append({
            "id": c.id, "code": c.current_code, "name": c.name, "phone": c.phone, "email": c.email,
            "category": st.category, "category_name": CATEGORY_NAMES[st.category], "reason": st.reason,
            "sub_slab": sub_slab(st), "sub_slab_label": sub_slab_label(sub_slab(st)),
            "rm": staff[c.rm_id].name if c.rm_id in staff else None,
            "dc": staff[c.dc_id].name if c.dc_id in staff else None,
            "next_action_at": _iso(c.next_action_at), "terminal_status": c.terminal_status,
            "docs": {t: {"status": s.status, "issue_date": _iso(s.issue_date), "expiry_date": _iso(s.expiry_date),
                         "days_left": s.days_left} for t, s in st.docs.items()},
            "gaps": c.contact_gaps or {},
        })
    return {"total": total, "page": page, "size": size, "rows": out}


@router.get("/csp/{csp_id}")
def csp_detail(csp_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    c = _own_csp(db, csp_id, me)
    staff = _staff_map(db)
    st = evaluate(db, c)
    docs = db.query(Document).filter_by(csp_id=c.id).order_by(Document.uploaded_at.desc()).all()
    msgs = db.query(OutboundMessage).filter_by(csp_id=c.id).order_by(OutboundMessage.created_at.desc()).limit(200).all()
    cycles = db.query(OutreachCycle).filter_by(csp_id=c.id).order_by(OutreachCycle.opened_at.desc()).all()
    inbound = db.query(InboundMessage).filter_by(csp_id=c.id).order_by(InboundMessage.received_at.desc()).limit(50).all()
    rm, dc = staff.get(c.rm_id), staff.get(c.dc_id)
    return {
        "csp": {"id": c.id, "code": c.current_code, "name": c.name, "phone": c.phone, "alt_phone": c.alt_phone,
                "email": c.email, "state": c.state, "branch": c.branch, "circle": c.circle,
                "terminal_status": c.terminal_status, "folder": str(csp_folder(c.current_code, c.name)),
                "rm": {"name": rm.name, "phone": rm.phone, "email": rm.email} if rm else None,
                "dc": {"name": dc.name, "phone": dc.phone, "email": dc.email} if dc else None,
                "gaps": c.contact_gaps or {}, "next_action_at": _iso(c.next_action_at)},
        "category": st.category, "category_name": CATEGORY_NAMES[st.category], "reason": st.reason,
        "current": {t: {"status": s.status, "document_id": s.document.id if s.document else None,
                        "issue_date": _iso(s.issue_date), "expiry_date": _iso(s.expiry_date), "days_left": s.days_left}
                    for t, s in st.docs.items()},
        "documents": [_doc_json(d) for d in docs],
        "messages": [_msg_json(m, c) for m in msgs],
        "cycles": [{"id": y.id, "kind": y.kind, "document_type": y.document_type, "ladder": y.ladder,
                    "anchor_date": _iso(y.anchor_date), "opened_at": _iso(y.opened_at), "closed_at": _iso(y.closed_at),
                    "close_reason": y.close_reason, "csp": y.csp_messages, "rm": y.rm_messages, "dc": y.dc_messages}
                   for y in cycles],
        "emails": [{"id": i.id, "subject": i.subject, "sender": i.sender, "received_at": _iso(i.received_at),
                    "status": i.status, "decisions": i.attachment_decisions or []} for i in inbound],
    }


@router.post("/csp/{csp_id}/upload-link")
def make_upload_link(csp_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    c = _own_csp(db, csp_id, me)
    st = evaluate(db, c)
    link = issue_upload_link(db, c, st.needs_upload or list(REQUIRED_TYPES))
    db.commit()
    return {"link": link}


@router.get("/csp/{csp_id}/zip")
def csp_zip(csp_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    c = _own_csp(db, csp_id, me)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in db.query(Document).filter_by(csp_id=c.id, is_current=True):
            p = vault.abs_path(d.storage_path)
            if p is not None and p.exists() and vault.is_inside_vault(p):
                z.write(p, p.name)
    buf.seek(0)
    name = csp_folder(c.current_code, c.name).name
    return StreamingResponse(buf, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


# ---------------------------------------------------------------- documents
@router.get("/documents")
def list_documents(doc_type: str = "", status: str = "", current_only: bool = True, channel: str = "",
                   page: int = 1, size: int = 100, me: Principal = Depends(require_user),
                   db: Session = Depends(get_db)):
    q = _mine(db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id), me)
    if channel:
        # Recent uploads: every copy received on this channel, newest first.
        q = q.filter(Document.upload_channel == channel)
        current_only = False
    if current_only:
        q = q.filter(or_(Document.is_current.is_(True), Document.readability == "UNREADABLE"))
    if doc_type:
        q = q.filter(Document.document_type == doc_type)
    if status:
        q = q.filter(Document.status == DocumentStatus(status))
    total = q.count()
    order = Document.uploaded_at.desc().nullslast() if channel else Document.expiry_date.asc().nullslast()
    rows = q.order_by(order).offset((page - 1) * size).limit(min(size, 500)).all()
    return {"total": total, "rows": [{**_doc_json(d), "csp_id": c.id, "csp_code": c.current_code, "csp_name": c.name}
                                     for d, c in rows]}


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: int, download: bool = False, me: Principal = Depends(require_user),
                  db: Session = Depends(get_db)):
    d = db.get(Document, doc_id)
    if d is not None:
        _own_csp(db, d.csp_id, me)
    path = vault.abs_path(d.storage_path) if d is not None else None
    if path is None or not path.exists():
        raise HTTPException(404, "file not found")
    path = path.resolve()
    if not vault.is_inside_vault(path):
        raise HTTPException(403, "file outside storage")
    media = d.mime_type if d.mime_type in ("application/pdf", "image/jpeg", "image/png") else "application/octet-stream"
    disp = "attachment" if download else "inline"
    return FileResponse(path, media_type=media, headers={
        "Content-Disposition": f'{disp}; filename="{path.name}"', "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------- communication hub
# Communication Hub groups: which templates make up each message type.
MESSAGE_KINDS = {
    "renewal": ("RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL"),
    "expired": ("UPLOAD_EXPIRED",),
    "missing": ("UPLOAD_MISSING", "UNREADABLE_REUPLOAD"),
    "onboard": ("ONBOARD_ALL",),
    "escalation": ("ESCALATION_RM", "ESCALATION_DC"),
}


@router.get("/messages")
def list_messages(channel: str = "", status: str = "", csp_id: Optional[int] = None, slab: str = "",
                  kind: str = "", page: int = 1, size: int = 50, me: Principal = Depends(require_user),
                  db: Session = Depends(get_db)):
    slab = int(slab) if slab.strip().isdigit() else None   # "" (All slabs) or a slab number
    base = db.query(OutboundMessage).outerjoin(CSP, CSP.id == OutboundMessage.csp_id)
    if not me.is_admin:
        base = base.filter(CSP.rm_id == me.user_id)
    if channel:
        base = base.filter(OutboundMessage.channel == channel.upper())
    if status:
        base = base.filter(OutboundMessage.status == OutboundStatus(status))
    if csp_id:
        base = base.filter(OutboundMessage.csp_id == csp_id)
    # Counts per slab and per message type, for the tabs (within channel + status).
    by_slab = dict(base.with_entities(CSP.category, func.count()).group_by(CSP.category).all())
    by_template = dict(base.with_entities(OutboundMessage.template_name, func.count())
                       .group_by(OutboundMessage.template_name).all())
    q = base
    if slab:
        q = q.filter(CSP.category == slab)
    if kind in MESSAGE_KINDS:
        q = q.filter(OutboundMessage.template_name.in_(MESSAGE_KINDS[kind]))
    total = q.count()
    rows = q.order_by(OutboundMessage.created_at.desc()).offset((page - 1) * size).limit(min(size, 200)).all()
    csps = {c.id: c for c in db.query(CSP).filter(CSP.id.in_({m.csp_id for m in rows} or {-1}))}
    counts = dict(_mine(db.query(OutboundMessage.status, func.count())
                        .outerjoin(CSP, CSP.id == OutboundMessage.csp_id), me)
                  .filter(OutboundMessage.channel == channel.upper() if channel else True)
                  .group_by(OutboundMessage.status).all())
    return {"total": total, "counts": {k.value: v for k, v in counts.items()},
            "by_slab": {str(k): by_slab.get(k, 0) for k in (1, 2, 3, 4)},
            "by_kind": {k: sum(by_template.get(t, 0) for t in ts) for k, ts in MESSAGE_KINDS.items()},
            "rows": [_msg_json(m, csps.get(m.csp_id)) for m in rows]}


def _get_msg(db: Session, message_id: int, me: Principal) -> OutboundMessage:
    m = db.get(OutboundMessage, message_id)
    if m is None or not (me.is_admin or (m.csp_id and db.get(CSP, m.csp_id).rm_id == me.user_id)):
        raise HTTPException(404, "message not found")
    return m


@router.post("/messages/{message_id}/approve")
def approve_message(message_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    m = _get_msg(db, message_id, me)
    reviewer = me.name
    try:
        outbound.approve(db, m, reviewer)
    except ValueError as e:
        raise HTTPException(409, str(e))
    outbound.send(db, m)
    db.commit()
    return _msg_json(m, db.get(CSP, m.csp_id))


@router.post("/messages/{message_id}/send-now")
def send_parked_message(message_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    m = _get_msg(db, message_id, me)
    try:
        outbound.send_parked(db, m, me.name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return _msg_json(m, db.get(CSP, m.csp_id))


@router.post("/messages/approve-bulk")
def approve_bulk(ids: list[int] = Body(..., embed=True), me: Principal = Depends(require_user),
                 db: Session = Depends(get_db)):
    done, reviewer = {}, me.name
    for mid in ids[:200]:
        m = db.get(OutboundMessage, mid)
        if m is None or m.status != OutboundStatus.QUEUED_FOR_REVIEW:
            continue
        if not me.is_admin and (m.csp_id is None or db.get(CSP, m.csp_id).rm_id != me.user_id):
            continue
        try:
            outbound.approve(db, m, reviewer)
        except ValueError as e:
            done["refused: " + str(e)[:120]] = done.get("refused: " + str(e)[:120], 0) + 1
            continue
        outbound.send(db, m)
        db.commit()
        done[m.status.value] = done.get(m.status.value, 0) + 1
    return {"result": done}


@router.post("/messages/{message_id}/reject")
def reject_message(message_id: int, reason: str = Body("", embed=True), me: Principal = Depends(require_user),
                   db: Session = Depends(get_db)):
    m = _get_msg(db, message_id, me)
    reviewer = me.name
    try:
        outbound.reject(db, m, reviewer, reason)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return _msg_json(m, db.get(CSP, m.csp_id))


@router.post("/messages/{message_id}/edit")
def edit_message(message_id: int, subject: Optional[str] = Body(None), body: Optional[str] = Body(None),
                 me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Text only: the recipient can never be changed from the dashboard."""
    m = _get_msg(db, message_id, me)
    try:
        outbound.edit_text(m, subject, body)
    except ValueError as e:
        raise HTTPException(409, str(e))
    db.commit()
    return _msg_json(m, db.get(CSP, m.csp_id))


# ------------------------------------------------------------------ reports
@router.get("/reports/{kind}.xlsx", dependencies=[Depends(require_admin)])
def report_xlsx(kind: str, db: Session = Depends(get_db)):
    """Live Excel: kind = csp (categories, documents, expiring), contacts (every
    CSP / RM / DC contact) or gaps (only what is missing)."""
    from .. import reports
    builders = {"csp": ("CSP_Report", lambda: reports.csp_report(db)),
                "contacts": ("Contacts", lambda: reports.contacts_report(db)),
                "gaps": ("Contact_Gaps", lambda: reports.gaps_report(db)),
                "unmatched": ("Unmatched_Emails", lambda: reports.unmatched_report(db))}
    if kind not in builders:
        raise HTTPException(404, "unknown report")
    prefix, build = builders[kind]
    name = f"{prefix}_{date.today().isoformat()}.xlsx"
    return StreamingResponse(build(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ contacts
@router.get("/contacts", dependencies=[Depends(require_admin)])
def contacts(db: Session = Depends(get_db)):
    staff = _staff_map(db)
    csp_rows, rm_rows, dc_rows = [], [], []
    for c in db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.current_code):
        g = c.contact_gaps or {}
        base = {"id": c.id, "code": c.current_code, "name": c.name}
        if g.get("csp"):
            csp_rows.append({**base, "phone": c.phone, "email": c.email, "missing": g["csp"]})
        if g.get("rm"):
            rm_rows.append({**base, "rm": staff[c.rm_id].name if c.rm_id in staff else None, "missing": g["rm"]})
        if g.get("dc"):
            dc_rows.append({**base, "dc": staff[c.dc_id].name if c.dc_id in staff else None, "missing": g["dc"]})
    people = [{"id": u.id, "name": u.name, "role": u.role, "phone": u.phone, "email": u.email,
               "csps": db.query(CSP).filter((CSP.rm_id == u.id) | (CSP.dc_id == u.id)).count()}
              for u in sorted(staff.values(), key=lambda u: (u.role or "", u.name)) if u.role in ("RM", "DC")]
    changes = [{"id": r.id, "csp_id": r.csp_id, "field": r.field, "old": r.old_value, "new": r.new_value,
                "created_at": _iso(r.created_at)}
               for r in db.query(ContactChangeRequest).filter_by(status="PENDING").order_by(ContactChangeRequest.id)]
    return {"csp": csp_rows, "rm": rm_rows, "dc": dc_rows, "staff": people, "change_requests": changes}


@router.post("/staff/{user_id}", dependencies=[Depends(require_admin)])
def update_staff(user_id: int, phone: Optional[str] = Body(None), email: Optional[str] = Body(None),
                 db: Session = Depends(get_db)):
    """RM contact details aren't in the calling-sheet tab, so they're entered
    here once. They become allowed recipients for that RM's CSPs."""
    from ..comms.sheets_sync import clean_phones, clean_email, refresh_contact_gaps
    u = db.get(InternalUser, user_id)
    if u is None or u.role not in ("RM", "DC"):
        raise HTTPException(404, "RM/DC not found")
    if phone is not None:
        phones = clean_phones(phone)
        if phone and not phones:
            raise HTTPException(422, "Enter a valid 10-digit mobile number.")
        u.phone = phones[0] if phones else None
    if email is not None:
        e = clean_email(email)
        if email and not e:
            raise HTTPException(422, "Enter a valid email address.")
        u.email = e
    refresh_contact_gaps(db)
    db.commit()
    return {"id": u.id, "name": u.name, "phone": u.phone, "email": u.email}


@router.post("/contact-changes/{req_id}/{action}", dependencies=[Depends(require_admin)])
def resolve_contact_change(req_id: int, action: str, db: Session = Depends(get_db)):
    r = db.get(ContactChangeRequest, req_id)
    if r is None or action not in ("done", "dismiss"):
        raise HTTPException(404, "not found")
    r.status = "DONE" if action == "done" else "DISMISSED"
    db.commit()
    return {"id": r.id, "status": r.status}


# ----------------------------------------------------------- review, inbound
@router.get("/review")
def review_queue(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    rows = (_mine(db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
                  .join(CSP, CSP.id == Document.csp_id), me).filter(ManualReviewQueue.status == ReviewStatus.PENDING)
            .order_by(ManualReviewQueue.created_at).all())
    return {"rows": [{"id": q.id, "reason": q.reason, "created_at": _iso(q.created_at), "document": _doc_json(d),
                      "csp": {"id": c.id, "code": c.current_code, "name": c.name}} for q, d, c in rows]}


@router.get("/accuracy", dependencies=[Depends(require_admin)])
def accuracy_stats(days: int = 90, db: Session = Depends(get_db)):
    from .. import accuracy
    return {**accuracy.stats(db, days), "mistakes": accuracy.mistakes(db)}


@router.post("/review/{item_id}/resolve")
def resolve_review(item_id: int, issue_date: Optional[str] = Body(None), accept: bool = Body(True),
                   me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    from ..compliance import refresh_category
    from ..expiry_engine import calculate_document_expiry
    reviewer = me.name
    q = db.get(ManualReviewQueue, item_id)
    if q is None:
        raise HTTPException(404, "not found")
    d = db.get(Document, q.document_id)
    _own_csp(db, d.csp_id, me)
    # "Corrected" only when the reviewer actually changed the date (the field
    # is pre-filled with the stored date): that is what accuracy counts.
    changed = bool(issue_date) and date.fromisoformat(issue_date) != d.issue_date
    if accept:
        if changed:
            d.issue_date = date.fromisoformat(issue_date)
            exp = calculate_document_expiry(canonical_type(d.document_type), d.issue_date,
                                            has_explicit_3year_clause=d.has_explicit_3year_clause,
                                            validity_months=d.validity_months)
            d.expiry_date = exp["calculated_expiry"]
            d.date_source = "MANUAL_REVIEW"
        d.status = DocumentStatus.MANUAL_VERIFIED
        q.status = ReviewStatus.CORRECTED if changed else ReviewStatus.APPROVED
        seen = (d.extracted_fields or {}).get("owner_seen_name")
        if (d.extracted_fields or {}).get("owner_check") and seen:
            # Teach the owner check this spelling of the CSP's name.
            owner = db.get(CSP, d.csp_id)
            db.add(ExtractionCorrection(document_id=d.id, field_name="owner_name", ai_value=seen,
                                        human_value=owner.name if owner else None))
    else:
        d.status = DocumentStatus.REJECTED
        q.status = ReviewStatus.REJECTED
    q.resolved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    q.correction_notes = f"by {reviewer}"
    from ..document_service import recompute_current, sync_agreement_row
    c = db.get(CSP, d.csp_id)
    recompute_current(db, c, d.document_type)
    sync_agreement_row(db, c)
    vault.place_csp(db, c)
    from ..renewal_engine import on_documents_received, run_for_csp
    if accept:
        on_documents_received(db, c)
    else:
        # The rejected copy no longer counts: ask the CSP again right away.
        run_for_csp(db, c)
    db.commit()
    return {"id": q.id, "status": q.status.value}


@router.get("/unmatched", dependencies=[Depends(require_admin)])
def unmatched(db: Session = Depends(get_db)):
    """Emails no CSP on the calling sheet matched, with the codes they mention."""
    from .. import reports
    return reports.unmatched_emails(db)


@router.get("/inbound", dependencies=[Depends(require_admin)])
def inbound(status: str = "", page: int = 1, size: int = 50, db: Session = Depends(get_db)):
    q = db.query(InboundMessage)
    if status:
        q = q.filter(InboundMessage.status == status)
    total = q.count()
    rows = q.order_by(InboundMessage.received_at.desc().nullslast()).offset((page - 1) * size).limit(min(size, 200)).all()
    return {"total": total, "rows": [{"id": i.id, "subject": i.subject, "sender": i.sender, "csp_id": i.csp_id,
                                      "received_at": _iso(i.received_at), "status": i.status, "note": i.error_message,
                                      "sender_on_sheet": i.sender_on_sheet, "decisions": i.attachment_decisions or []}
                                     for i in rows]}


# --------------------------------------------------------------------- jobs
def _run_job(name: str, fn):
    def target():
        _jobs[name] = {"status": "RUNNING", "started_at": datetime.now().isoformat(timespec="seconds")}
        try:
            result = fn()
            _jobs[name].update(status="DONE", result=result)
        except Exception as e:
            logger.exception("manual_job_failed %s", name)
            _jobs[name].update(status="FAILED", error=f"{type(e).__name__}: {e}")
        _jobs[name]["finished_at"] = datetime.now().isoformat(timespec="seconds")
    if _jobs.get(name, {}).get("status") == "RUNNING":
        raise HTTPException(409, f"{name} is already running")
    threading.Thread(target=target, daemon=True).start()


@router.post("/run/{job}", dependencies=[Depends(require_admin)])
def run_job(job: str):
    from .. import worker
    jobs = {"sheet": worker.job_sheet, "engine": worker.job_engine, "gmail": worker.job_gmail,
            "outbox": worker.job_outbox, "vault": worker.job_vault, "reports": worker.job_reports}
    if job not in jobs:
        raise HTTPException(404, "unknown job")
    _run_job(job, jobs[job])
    return {"started": job}
