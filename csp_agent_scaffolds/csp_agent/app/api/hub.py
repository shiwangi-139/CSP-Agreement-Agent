"""
app/api/hub.py
Data for the dashboard (app/web/dashboard.html). All routes need X-API-Key.
"""
import io
import logging
import threading
import zipfile
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..auth import Principal, require_admin, require_user
from ..compliance import (CATEGORY_NAMES, INACTIVE_LABEL, inactive_clause, DOC_LABELS, REQUIRED_TYPES, SUB_SLABS, canonical_type, evaluate,
                          sub_slab, sub_slab_label)
from ..comms import outbound
from ..config import (OUTBOUND_COMMUNICATION_MODE, WHATSAPP_MODE, CALLING_SHEET_LINK, CALLING_SHEET_SOURCE, local_now,
                      FEATURE_MESSAGING_ANALYTICS,
                      CALLING_SHEET_TAB)
from ..db import get_db, SessionLocal
from ..models import (AgreementEvent, CSP, ContactChangeRequest, CspQuestion, Document, DocumentStatus, ExtractionCorrection, InboundMessage, InternalUser,
                      ManualReviewQueue, OutboundMessage, OutboundStatus, OutreachCycle, ReviewStatus)
from ..portal_tokens import issue_upload_link
from .. import vault
from ..vault import csp_folder

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_user)])

_jobs: dict[str, dict] = {}


# The test CSP (scripts/test_csp.py, state "TEST") never shows on the dashboard:
# its messages, uploads and questions are tests, not work.
NOT_TEST = or_(CSP.state.is_(None), CSP.state != "TEST")


def _rm_filter(me: Principal):
    """An RM's CSPs; the RM named in TBA_CSPS_RM also gets those with no RM."""
    return or_(CSP.rm_id == me.user_id, CSP.rm_id.is_(None)) if me.sees_unassigned else CSP.rm_id == me.user_id


def _can_see(me: Principal, csp: Optional[CSP]) -> bool:
    return me.is_admin or (csp is not None and (csp.rm_id == me.user_id
                                                or (me.sees_unassigned and csp.rm_id is None)))


def _mine(query, me: Principal):
    """An RM sees only their own CSPs (plus the TBA ones, for the RM named in
    TBA_CSPS_RM); an admin sees all; nobody sees the test CSP. `query` must
    involve the CSP table."""
    query = query.filter(NOT_TEST)
    return query if me.is_admin else query.filter(_rm_filter(me))


def _own_csp(db: Session, csp_id: Optional[int], me: Principal) -> CSP:
    c = db.get(CSP, csp_id) if csp_id else None
    if c is None or not _can_see(me, c):
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
            "model_suggestion": f.get("model_suggestion"),
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
    on_sheet = _mine(db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)), me)
    inactive = on_sheet.filter(inactive_clause()).count()
    active = on_sheet.filter(~inactive_clause())          # Labels 1-4: working terminals only
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
        "categories": {**{str(k): cats.get(k, 0) for k in (1, 2, 3, 4)}, str(INACTIVE_LABEL): inactive},
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
        "questions_open": _mine(db.query(CspQuestion).join(CSP, CSP.id == CspQuestion.csp_id), me)
                          .filter(CspQuestion.status == "OPEN").count(),
        "form_uploads": _form_upload_counts(db, me),
        "me": {"name": me.name, "role": me.role},
        "features": {"analytics": FEATURE_MESSAGING_ANALYTICS},
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
    if category == INACTIVE_LABEL:
        query = query.filter(inactive_clause())
    else:
        query = query.filter(~inactive_clause())
    if category and category != INACTIVE_LABEL:
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


# ------------------------------------------------- find CSPs by document
# Each document of each CSP is in one state; any combination can be asked
# for ("only the agreement expired" = agreement EXPIRED, PVR VALID, IIBF VALID).
DOC_STATES = ("VALID", "EXPIRING", "EXPIRED", "MISSING", "UNREADABLE")
DOC_PARAM = {"agr": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}
EXPIRING_DAYS = 60


def _doc_state(s) -> str:
    if s.status == "VALID" and s.days_left is not None and 0 <= s.days_left <= EXPIRING_DAYS:
        return "EXPIRING"
    return s.status


def _form_outcomes(db: Session, ids: list[int]) -> dict[int, str]:
    """Each CSP's latest upload-page outcome (accepted / partly / rejected)."""
    out: dict[int, str] = {}
    for e in (db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD",
                                               AgreementEvent.csp_id.in_(ids or [-1]))
              .order_by(AgreementEvent.sent_at.desc())):
        out.setdefault(e.csp_id, _outcome((e.payload or {}).get("results") or []))
    return out


def _find(db: Session, me: Principal, agr: str = "", pvr: str = "", iibf: str = "", slab: Optional[int] = None,
          rm: str = "", form: str = "") -> list[tuple]:
    query = _mine(db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)), me)
    if slab == INACTIVE_LABEL:
        query = query.filter(inactive_clause())
    else:
        query = query.filter(~inactive_clause())
    if slab and slab != INACTIVE_LABEL:
        query = query.filter(CSP.category == slab)
    staff = _staff_map(db)
    if rm:
        ids = [u.id for u in staff.values() if u.role == "RM" and u.name.lower() == rm.lower()]
        query = query.filter(CSP.rm_id.in_(ids or [-1]))
    csps = query.order_by(CSP.current_code).all()
    forms = _form_outcomes(db, [c.id for c in csps])
    want = {DOC_PARAM[k]: v.upper() for k, v in (("agr", agr), ("pvr", pvr), ("iibf", iibf)) if v}
    out = []
    for c in csps:
        if form == "yes" and c.id not in forms or form == "no" and c.id in forms \
                or form in ("accepted", "partly", "rejected") and forms.get(c.id) != form:
            continue
        st = evaluate(db, c)
        states = {t: _doc_state(x) for t, x in st.docs.items()}
        if all(states.get(t) == v for t, v in want.items()):
            out.append((c, st, states, forms.get(c.id), staff))
    return out


def _find_row(c, st, states, form, staff) -> dict:
    return {"id": c.id, "code": c.current_code, "name": c.name, "phone": c.phone,
            "category": st.category, "sub_slab": sub_slab(st), "sub_slab_label": sub_slab_label(sub_slab(st)),
            "rm": staff[c.rm_id].name if c.rm_id in staff else None,
            "dc": staff[c.dc_id].name if c.dc_id in staff else None, "form": form,
            "docs": {t: {"status": x.status, "state": states[t], "issue_date": _iso(x.issue_date),
                         "expiry_date": _iso(x.expiry_date), "days_left": x.days_left} for t, x in st.docs.items()}}


@router.get("/find")
def find_csps(agr: str = "", pvr: str = "", iibf: str = "", slab: Optional[int] = None, rm: str = "", form: str = "",
              page: int = 1, size: int = 50, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    found = _find(db, me, agr, pvr, iibf, slab, rm, form)
    size = min(max(size, 1), 200)
    rows = found[(page - 1) * size: page * size]
    return {"total": len(found), "page": page, "size": size, "rows": [_find_row(*r) for r in rows]}


@router.get("/find.xlsx")
def find_csps_xlsx(agr: str = "", pvr: str = "", iibf: str = "", slab: Optional[int] = None, rm: str = "",
                   form: str = "", me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "CSPs"
    head = ["CSP code", "Name", "Phone", "RM", "DC", "Label", "Sub-group"]
    for t in REQUIRED_TYPES:
        en = DOC_LABELS[t][0]
        head += [f"{en}: state", f"{en}: issued", f"{en}: expires"]
    ws.append(head + ["Upload page"])
    for r in (_find_row(*x) for x in _find(db, me, agr, pvr, iibf, slab, rm, form)):
        line = [r["code"], r["name"], r["phone"], r["rm"], r["dc"], r["category"], r["sub_slab_label"]]
        for t in REQUIRED_TYPES:
            d = r["docs"][t]
            line += [d["state"].lower(), d["issue_date"], d["expiry_date"]]
        ws.append(line + [r["form"] or ""])
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = "csps_" + "_".join(f"{k}-{v}" for k, v in (("agreement", agr), ("pvr", pvr), ("iibf", iibf), ("label", slab),
                                                         ("rm", rm), ("form", form)) if v) or "csps_all"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name[:80]}.xlsx"'})


@router.get("/doc-matrix")
def doc_matrix(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Counts of CSPs per document and state, for the overview table."""
    counts = {t: {k: 0 for k in DOC_STATES} for t in REQUIRED_TYPES}
    for c, st, states, _, _ in _find(db, me):
        for t, v in states.items():
            if t in counts and v in counts[t]:
                counts[t][v] += 1
    return {"counts": counts, "expiring_days": EXPIRING_DAYS,
            "rms": sorted(u.name for u in _staff_map(db).values() if u.role == "RM") if me.is_admin else []}


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
                "preferred_phone": c.preferred_phone, "preferred_phone_set_by": c.preferred_phone_set_by,
                "preferred_phone_set_at": _iso(c.preferred_phone_set_at),
                "email": c.email, "state": c.state, "branch": c.branch, "circle": c.circle,
                "terminal_status": c.terminal_status, "folder": str(csp_folder(c.current_code, c.name)),
                "rm": {"name": rm.name, "phone": rm.phone, "email": rm.email} if rm else None,
                "dc": {"name": dc.name, "phone": dc.phone, "email": dc.email} if dc else None,
                "gaps": c.contact_gaps or {}, "next_action_at": _iso(c.next_action_at)},
        "category": st.category, "category_name": CATEGORY_NAMES[st.category], "reason": st.reason,
        "current": {t: {"status": s.status, "document_id": s.document.id if s.document else None,
                        "issue_date": _iso(s.issue_date), "expiry_date": _iso(s.expiry_date), "days_left": s.days_left,
                        "applied_on": _iso(s.applied_on)}
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


@router.post("/csp/{csp_id}/whatsapp-number")
def set_whatsapp_number(csp_id: int, phone: str = Body("", embed=True), me: Principal = Depends(require_user),
                        db: Session = Depends(get_db)):
    """The number the CSP asked us to use for WhatsApp (after a call). Empty =
    back to the calling sheet's numbers. Unsent WhatsApp messages to another
    number are retired and drafted again to this one."""
    from ..comms.outbound import _phone10, use_preferred_phone
    c = _own_csp(db, csp_id, me)
    if phone.strip():
        p = _phone10(phone)
        if not p or not re.fullmatch(r"[6-9]\d{9}", p):
            raise HTTPException(422, "Enter a 10-digit Indian mobile number.")
        c.preferred_phone, c.preferred_phone_set_by = p, me.name
        c.preferred_phone_set_at = datetime.now(timezone.utc).replace(tzinfo=None)
    else:
        c.preferred_phone = c.preferred_phone_set_by = c.preferred_phone_set_at = None
    retired = use_preferred_phone(db, c)
    db.commit()
    return {"preferred_phone": c.preferred_phone, "retired": retired,
            "waiting": [m.destination for m in db.query(OutboundMessage).filter(
                OutboundMessage.csp_id == c.id, OutboundMessage.channel == "WHATSAPP",
                OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW)]}


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
    name = path.name
    if not download and media != "application/octet-stream":
        # Viewing: a light copy of a big scan (app/view_copy.py); download = the original.
        from ..view_copy import view_file
        path, copy_media = view_file(d.id, path)
        media = copy_media or media
    return FileResponse(path, media_type=media, headers={
        "Content-Disposition": f'{disp}; filename="{name}"', "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------- communication hub
# Communication Hub groups: which templates make up each message type.
MESSAGE_KINDS = {
    "renewal": ("RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL"),
    "expired": ("UPLOAD_EXPIRED",),
    "missing": ("UPLOAD_MISSING", "UNREADABLE_REUPLOAD"),
    "onboard": ("ONBOARD_ALL",),
    "escalation": ("ESCALATION_RM", "ESCALATION_DC"),
    "upload_reply": ("UPLOAD_RECEIPT", "UPLOAD_SUCCESS_ALL", "UPLOAD_PARTIAL_REJECTED"),
}


@router.get("/messages")
def list_messages(channel: str = "", status: str = "", csp_id: Optional[int] = None, slab: str = "",
                  kind: str = "", page: int = 1, size: int = 50, me: Principal = Depends(require_user),
                  db: Session = Depends(get_db)):
    slab = int(slab) if slab.strip().isdigit() else None   # "" (All slabs) or a slab number
    base = db.query(OutboundMessage).outerjoin(CSP, CSP.id == OutboundMessage.csp_id).filter(NOT_TEST)
    if not me.is_admin:
        base = base.filter(_rm_filter(me))
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
    # Locked until this request commits: a second click (or a second tab) waits,
    # then sees the message already approved and is refused, so it is sent once.
    m = db.query(OutboundMessage).filter(OutboundMessage.id == message_id).with_for_update().first()
    if m is None or not (me.is_admin or (m.csp_id and _can_see(me, db.get(CSP, m.csp_id)))):
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
        m = db.query(OutboundMessage).filter(OutboundMessage.id == mid).with_for_update().first()   # see _get_msg
        if m is None or m.status != OutboundStatus.QUEUED_FOR_REVIEW:
            continue
        if not me.is_admin and (m.csp_id is None or not _can_see(me, db.get(CSP, m.csp_id))):
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


# ------------------------------------------------ "Label 5": form uploads
# Everyone who used the upload page, on top of their slab 1-4. One row per
# submission (AgreementEvent PORTAL_UPLOAD, written by app/api/portal.py).
REASON_TEXT = {
    "accepted": "accepted", "review": "accepted, date checked by the team", "duplicate": "already on file",
    "unreadable": "unreadable (counts as missing)", "received_check": "received, our team is checking it", "wrong_doc": "wrong document", "expired": "expired",
    "application_only": "application receipt, not the certificate",
    "iibf_receipt": "IIBF fee receipt, not the certificate",
    "dc_valid": "already valid (master link: not needed yet)", "dc_in_review": "already being checked",
    "dup_other": "file already on record for another CSP", "dup_same": "same document as the one on file",
    "dc_other_csp": "document of another CSP (master link, wrong KO code)",
    "pvr_applied": "PVR application recorded (applied, waiting for the certificate)",
    "agreement_short": "agreement incomplete (only 1-4 pages)",
    "dup_rejected": "same file as one rejected before", "dup_unreadable": "same file as one unreadable before",
    "too_small": "photo too small", "too_dark": "photo too dark", "washed_out": "photo washed out",
    "blurry": "photo blurry", "no_date": "no issue date", "future": "date in the future",
    "too_big": "file too large", "file_type": "not a PDF/JPG/PNG",
}
SECTION_TYPE = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}


ACCEPTED_NOW = {DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED, DocumentStatus.NEEDS_APPROVAL,
                DocumentStatus.EXTRACTED_DETERMINISTIC, DocumentStatus.EXTRACTED_AI}


def _now_states(db: Session, results: list[dict], csp: Optional[CSP] = None) -> list[dict]:
    """Each result as it stands NOW: a document later accepted (re-check or
    review), still in review, or rejected by the team, overrides how the
    upload went at the time. And if the CSP has a valid copy of that
    document on record now (from any source), it counts as accepted."""
    on_record = set()
    if csp is not None:
        st = evaluate(db, csp)
        on_record = {t for t, x in st.docs.items() if x.status == "VALID"}
    out = []
    for r in results:
        r = dict(r)
        if not r.get("ok") and SECTION_TYPE.get(r.get("section")) in on_record:
            r["ok"], r["now"] = True, "a valid copy is on record now"
            out.append(r)
            continue
        did = r.get("rechecked_document_id") or r.get("document_id")
        d = db.get(Document, did) if did else None
        now = None
        if d is not None and d.readability == "READABLE":
            if d.status in ACCEPTED_NOW or (d.status == DocumentStatus.EXPIRED and r.get("ok")):
                now = "accepted"
            elif d.status == DocumentStatus.NEEDS_REVIEW:
                now = "in_review"
            elif d.status == DocumentStatus.REJECTED:
                now = "rejected_by_team"
        if now == "accepted" and not r.get("ok"):
            r["ok"], r["now"] = True, "accepted later (re-check / review)"
        elif now == "in_review":
            r["now"], r["in_review"] = "our team is checking it", True
        elif now == "rejected_by_team" and r.get("ok"):
            r["ok"], r["now"] = False, "rejected by our team on review"
        out.append(r)
    return out


def _outcome(results: list[dict]) -> str:
    """accepted / checking (nothing turned away, something still waits for
    a person) / partly / rejected."""
    oks = [bool(r.get("ok")) for r in results]
    if oks and all(oks):
        return "checking" if any(r.get("in_review") for r in results) else "accepted"
    return "rejected" if not any(oks) else "partly"


def _form_events(db: Session, me: Principal, days: int = 0):
    q = _mine(db.query(AgreementEvent, CSP).join(CSP, CSP.id == AgreementEvent.csp_id), me) \
        .filter(AgreementEvent.event_type == "PORTAL_UPLOAD")
    if days:
        q = q.filter(AgreementEvent.sent_at >= local_now() - timedelta(days=days))     # local time (database clock)
    return q.order_by(AgreementEvent.sent_at.desc())


def _form_upload_counts(db: Session, me: Principal) -> dict:
    latest: dict[int, str] = {}
    for e, c in _form_events(db, me):                         # newest first: keep each CSP's latest
        if c.id not in latest:
            latest[c.id] = _outcome(_now_states(db, (e.payload or {}).get("results") or [], c))
    return {"csps": len(latest), "accepted": sum(v == "accepted" for v in latest.values()),
            "checking": sum(v == "checking" for v in latest.values()),
            "partly": sum(v == "partly" for v in latest.values()),
            "rejected": sum(v == "rejected" for v in latest.values())}


@router.get("/form-uploads")
def form_uploads(outcome: str = "", days: int = 0, me: Principal = Depends(require_user),
                 db: Session = Depends(get_db)):
    rms = {u.id: u.name for u in db.query(InternalUser).filter(InternalUser.role == "RM")}
    rows = []
    for e, c in _form_events(db, me, days).limit(1000):
        p = e.payload or {}
        results = _now_states(db, p.get("results") or [], c)
        out = _outcome(results)
        if outcome and out != outcome:
            continue
        rows.append({
            "id": e.id, "at": _iso(e.sent_at), "outcome": out,
            "csp": {"id": c.id, "code": c.current_code, "name": c.name, "slab": c.category, "sub_slab": c.sub_slab},
            "rm": rms.get(c.rm_id), "slab_before": p.get("slab_before"), "slab_after": p.get("slab_after"),
            "contact_changes": p.get("contact_changes") or 0,
            "results": [{"i": i, "section": r.get("section"),
                         "type": SECTION_TYPE.get(r.get("section"), r.get("section")), "ok": bool(r.get("ok")),
                         "reason": REASON_TEXT.get(r.get("reason"), "accepted" if r.get("ok") else "rejected"),
                         "read_as": r.get("read_as"), "filename": r.get("filename"),
                         "document_id": r.get("rechecked_document_id") or r.get("document_id"),
                         "kept_file": bool(r.get("kept_file")), "now": r.get("now"),
                         "in_review": bool(r.get("in_review"))}
                        for i, r in enumerate(results)]})
    return {"rows": rows[:500], "counts": _form_upload_counts(db, me)}


@router.get("/form-uploads/{event_id}/file/{i}")
def form_upload_file(event_id: int, i: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """A file the upload page turned away (kept under _portal_rejected/)."""
    e = db.get(AgreementEvent, event_id)
    if e is None or e.event_type != "PORTAL_UPLOAD":
        raise HTTPException(404, "not found")
    _own_csp(db, e.csp_id, me)
    results = (e.payload or {}).get("results") or []
    kept = results[i].get("kept_file") if 0 <= i < len(results) else None
    path = vault.abs_path(kept) if kept else None
    if path is None or not path.exists() or not vault.is_inside_vault(path.resolve()):
        raise HTTPException(404, "file not found")
    media = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    return FileResponse(path.resolve(), media_type=media.get(path.suffix.lower(), "application/octet-stream"),
                        headers={"Content-Disposition": f'inline; filename="{path.name}"',
                                 "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------ messaging analytics
@router.get("/analytics")
def messaging_analytics(days: int = 14, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Sent, delivered and what CSPs did afterwards, per India day. An RM
    sees only their own CSPs."""
    from .. import analytics
    if not FEATURE_MESSAGING_ANALYTICS:
        raise HTTPException(404, "Messaging analytics is switched off (FEATURE_MESSAGING_ANALYTICS).")
    ids = None if me.is_admin else {i for (i,) in _mine(db.query(CSP.id), me)}
    return analytics.build(db, ids, days=min(max(days, 1), 60))


# ------------------------------------------------------------ CSP questions
@router.get("/questions")
def list_questions(status: str = "OPEN", me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Questions CSPs asked on the upload page; an RM sees their own CSPs'."""
    q = _mine(db.query(CspQuestion, CSP).join(CSP, CSP.id == CspQuestion.csp_id), me)
    if status:
        q = q.filter(CspQuestion.status == status)
    rows = q.order_by(CspQuestion.created_at.desc()).limit(300).all()
    rms = {u.id: u.name for u in db.query(InternalUser).filter(InternalUser.role == "RM")}
    return {"rows": [{"id": x.id, "category": x.category, "text": x.text, "status": x.status,
                      "callback_phone": x.callback_phone, "created_at": _iso(x.created_at),
                      "answered_at": _iso(x.answered_at), "answered_by": x.answered_by, "answer_note": x.answer_note,
                      "rm": rms.get(c.rm_id), "csp": {"id": c.id, "code": c.current_code, "name": c.name,
                                                      "phone": c.phone}} for x, c in rows]}


@router.post("/questions/{qid}/answer")
def answer_question(qid: int, note: str = Body("", embed=True), me: Principal = Depends(require_user),
                    db: Session = Depends(get_db)):
    x = db.get(CspQuestion, qid)
    if x is None:
        raise HTTPException(404, "not found")
    _own_csp(db, x.csp_id, me)
    x.status, x.answered_at, x.answered_by = "ANSWERED", datetime.now(timezone.utc).replace(tzinfo=None), me.name
    x.answer_note = (note or "").strip()[:1000] or None
    db.commit()
    return {"id": x.id, "status": x.status}


# ----------------------------------------------------------- review, inbound
@router.get("/review")
def review_queue(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    rows = (_mine(db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
                  .join(CSP, CSP.id == Document.csp_id), me).filter(ManualReviewQueue.status == ReviewStatus.PENDING)
            .order_by(ManualReviewQueue.created_at).all())
    return {"rows": [{"id": q.id, "reason": q.reason, "created_at": _iso(q.created_at), "document": _doc_json(d),
                      "csp": {"id": c.id, "code": c.current_code, "name": c.name}} for q, d, c in rows]}


@router.get("/monitor/{tab}", dependencies=[Depends(require_admin)])
def monitor(tab: str, days: int = 0, db: Session = Depends(get_db)):
    """Monitoring page (admins only): one tab's numbers (app/monitoring.py)."""
    from .. import monitoring
    fn = monitoring.TABS.get(tab)
    if fn is None:
        raise HTTPException(404, "unknown tab")
    return fn(db, days=min(max(days, 1), 90)) if days and "days" in fn.__code__.co_varnames else fn(db)


@router.get("/master-link", dependencies=[Depends(require_admin)])
def master_link_status(db: Session = Depends(get_db)):
    from .. import master_link
    return {**master_link.status(db), "dcs": _dc_names(db)}


def _dc_names(db: Session) -> list[dict]:
    from sqlalchemy import func
    from ..compliance import working_clause
    counts = dict(db.query(CSP.dc_id, func.count(CSP.id)).filter(working_clause(), CSP.dc_id.isnot(None))
                  .group_by(CSP.dc_id).all())
    out = [{"id": u.id, "name": u.name, "csps": counts.get(u.id, 0)}
           for u in db.query(InternalUser).filter(InternalUser.role == "DC").order_by(InternalUser.name)
           if counts.get(u.id)]
    no_dc = db.query(func.count(CSP.id)).filter(working_clause(), CSP.dc_id.is_(None)).scalar()
    if no_dc:
        out.append({"id": 0, "name": "No DC", "csps": no_dc})          # dc_id=0: CSPs with no DC
    return out


def _dc_or_404(db: Session, dc_id: int):
    from .. import reports
    if dc_id == 0:
        return reports.NO_DC
    dc = db.get(InternalUser, dc_id)
    if dc is None or dc.role != "DC":
        raise HTTPException(404, "No such DC")
    return dc


@router.get("/dc-progress", dependencies=[Depends(require_admin)])
def dc_progress(db: Session = Depends(get_db)):
    """Per DC: documents valid and CSPs complete on the previous day and now."""
    from .. import reports
    return reports.dc_progress(db)


@router.get("/dc-summary", dependencies=[Depends(require_admin)])
def dc_summary(dc_id: int, db: Session = Depends(get_db)):
    """One DC's CSPs as a short text to copy into WhatsApp."""
    from .. import reports
    dc = _dc_or_404(db, dc_id)
    return {"text": reports.dc_summary_text(db, dc)}


@router.get("/dc-report.xlsx", dependencies=[Depends(require_admin)])
def dc_report_xlsx(dc_id: int, db: Session = Depends(get_db)):
    """One DC's CSPs in plain words, to share with that DC."""
    from .. import reports
    dc = _dc_or_404(db, dc_id)
    name = f"CSPs_DC_{re.sub(r'[^A-Za-z0-9]+', '_', dc.name).strip('_')}_{date.today().isoformat()}.xlsx"
    return StreamingResponse(reports.dc_report(db, dc), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/master-link", dependencies=[Depends(require_admin)])
def master_link_create(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """A new DC master link (the old one stops working). The URL is shown once."""
    from .. import master_link
    url = master_link.create(db, me.name)
    db.commit()
    return {"url": url, **master_link.status(db)}


@router.get("/dc-sheet-key", dependencies=[Depends(require_admin)])
def dc_sheet_key(db: Session = Depends(get_db)):
    """The key for the DCs' live Google Sheets script (app/dc_sheets.py)."""
    from .. import dc_sheets
    row = dc_sheets.current(db)
    return {"key": row.token if row else None, "created_at": _iso(row.created_at) if row else None,
            "server": "https://indev.eko.in/csp-agreement-agent/upload"}


@router.post("/dc-sheet-key", dependencies=[Depends(require_admin)])
def dc_sheet_key_new(me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    from .. import dc_sheets
    dc_sheets.create(db, me.name)
    db.commit()
    return dc_sheet_key(db)


@router.post("/master-link/restore", dependencies=[Depends(require_admin)])
def master_link_restore(body: dict = Body(...), me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Bring back the link already shared (pasted by the admin) as the one live link."""
    from .. import master_link
    url = master_link.restore(db, str(body.get("url") or ""), me.name)
    if url is None:
        raise HTTPException(404, "This is not a master link made on this dashboard.")
    db.commit()
    return {"url": url, **master_link.status(db)}


@router.post("/master-link/revoke", dependencies=[Depends(require_admin)])
def master_link_revoke(db: Session = Depends(get_db)):
    from .. import master_link
    n = master_link.revoke_all(db)
    db.commit()
    return {"revoked": n}


@router.get("/accuracy", dependencies=[Depends(require_admin)])
def accuracy_stats(days: int = 90, db: Session = Depends(get_db)):
    from .. import accuracy
    return {**accuracy.stats(db, days), "mistakes": accuracy.mistakes(db)}


@router.post("/review/{item_id}/reread")
def reread_review(item_id: int, me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Read a waiting document again with the current rules (no model). Only
    its dates and the note change; the reviewer still accepts or rejects."""
    from ..review_tools import reread_waiting
    q = db.get(ManualReviewQueue, item_id)
    if q is None or q.status != ReviewStatus.PENDING:
        raise HTTPException(404, "not waiting")
    d = db.get(Document, q.document_id)
    _own_csp(db, d.csp_id, me)
    r = reread_waiting(db, q, d)
    db.commit()
    return {"changed": r["changed"], "note": r["note"]}


# Validity a reviewer may pick, in months (what the document states).
# -1 = no expiry (a Police Clearance Certificate states no validity).
NO_EXPIRY = -1
VALIDITY_CHOICES = {"AGREEMENT": (12, 24, 36, 60), "POLICE_VERIFICATION": (6, 12, NO_EXPIRY)}


@router.post("/review/{item_id}/resolve")
def resolve_review(item_id: int, issue_date: Optional[str] = Body(None), accept: bool = Body(True),
                   validity_months: Optional[int] = Body(None), document_type: Optional[str] = Body(None),
                   expiry_date: Optional[str] = Body(None),
                   me: Principal = Depends(require_user), db: Session = Depends(get_db)):
    """Accept (with the issue date, and optionally the validity the document
    states and its real type) or reject a document waiting in the queue."""
    from ..compliance import refresh_category
    from ..expiry_engine import add_months, add_years, calculate_document_expiry
    reviewer = me.name
    q = db.get(ManualReviewQueue, item_id)
    if q is None:
        raise HTTPException(404, "not found")
    d = db.get(Document, q.document_id)
    _own_csp(db, d.csp_id, me)
    if accept and d.issue_date is None and not issue_date:
        raise HTTPException(422, "Enter the document's issue date before accepting it (or reject it if the date is blank).")
    new_type = canonical_type(document_type) if accept and document_type else None
    if new_type and new_type not in REQUIRED_TYPES:
        raise HTTPException(422, "Unknown document type.")
    if new_type == canonical_type(d.document_type):
        new_type = None
    if accept and validity_months and \
            validity_months not in VALIDITY_CHOICES.get(new_type or canonical_type(d.document_type), ()):
        raise HTTPException(422, "Choose one of the validity periods offered for this document.")
    # "Corrected" only when the reviewer actually changed something (the date
    # field is pre-filled with the stored date): that is what accuracy counts.
    date_changed = bool(issue_date) and date.fromisoformat(issue_date) != d.issue_date
    # A last day written on the document (anywhere on it), typed by the reviewer.
    new_expiry = date.fromisoformat(expiry_date) if accept and expiry_date else None
    if new_expiry and new_expiry == d.expiry_date:
        new_expiry = None
    if new_expiry:
        start = date.fromisoformat(issue_date) if issue_date else d.issue_date
        if start and new_expiry <= start:
            raise HTTPException(422, "The expiry date must be after the issue date.")
    changed = date_changed or bool(new_type) or bool(new_expiry) or bool(validity_months and validity_months != d.validity_months)
    old_type = d.document_type
    if accept:
        if new_type:
            d.document_type = new_type
        if date_changed:
            d.issue_date = date.fromisoformat(issue_date)
            d.date_source = "MANUAL_REVIEW"
        if validity_months == NO_EXPIRY:
            d.validity_months, d.expiry_date, d.validity_rule_used = None, None, "PVR_PCC_LIFETIME"
        elif validity_months:
            # The validity the document states, as the reviewer read it.
            d.validity_months = validity_months
            d.has_explicit_3year_clause = validity_months == 36
            d.expiry_date = (add_years(d.issue_date, validity_months // 12) if validity_months % 12 == 0
                             else add_months(d.issue_date, validity_months))
            d.validity_rule_used = f"MANUAL_REVIEW_{validity_months}_MONTHS"
        elif date_changed or new_type:
            exp = calculate_document_expiry(canonical_type(d.document_type), d.issue_date,
                                            has_explicit_3year_clause=d.has_explicit_3year_clause,
                                            validity_months=d.validity_months,
                                            # a clearance certificate keeps "no expiry" when only its date changes
                                            validity_rule=d.validity_rule_used if d.validity_rule_used == "PVR_PCC_LIFETIME" else None)
            d.expiry_date = exp["calculated_expiry"]
        if new_expiry:
            d.expiry_date, d.validity_rule_used = new_expiry, "MANUAL_REVIEW_EXPIRY_DATE"
            d.validity_months = None
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
    if old_type != d.document_type:
        recompute_current(db, c, old_type)
    sync_agreement_row(db, c)
    vault.place_csp(db, c)
    from ..renewal_engine import on_documents_received, run_for_csp
    if accept:
        on_documents_received(db, c)
    else:
        # The rejected copy no longer counts: ask the CSP again right away.
        run_for_csp(db, c)
    db.commit()
    # A CSP's own upload that waited for a person: tell them the outcome now
    # (not for spot / reference checks of documents already accepted).
    # (A master-link listing of a document that already counts: accepting it
    # changes nothing for the CSP, so no second message.)
    dc_listing = (q.reason or "").startswith("DC upload") and "already counted" in (q.reason or "")[:80]
    if d.upload_channel in ("CSP_UPLOAD_PORTAL", "DC_MASTER_LINK") \
            and not (q.reason or "").startswith(("Spot check", "Reference check")) and not (dc_listing and accept):
        from ..comms.upload_receipt import send_upload_receipt
        from sqlalchemy.orm import sessionmaker
        send_upload_receipt(c.id, [{"type": canonical_type(d.document_type), "ok": accept,
                                    "reason": "accepted" if accept else "rejected_by_team"}],
                            session_factory=sessionmaker(bind=db.get_bind()))
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
