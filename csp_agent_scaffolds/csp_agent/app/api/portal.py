"""
app/api/portal.py
Mobile upload portal for CSPs (Hindi + English), reached from the link in
the WhatsApp / email messages.

GET  /upload?token=...          the page (app/web/portal.html); with no token
                                or a dead one: "get your upload link" (app/web/get_link.html)
POST /upload                    KO code + mobile -> a fresh link by WhatsApp, only to
                                a number on the calling sheet (never shown on screen);
                                or, with action=question and a live token, a question
                                for the CSP's RM (CspQuestion, shown on the dashboard)
GET  /api/portal/context        what this CSP still needs, prefilled contacts
POST /api/portal/upload         one or more documents + typed issue dates

Server-side rules (the page enforces the same, but the server decides):
  - PDF, JPG or PNG only, judged from the file bytes; size limit streamed
  - issue date required; not in the future; not already expired
    (issue + validity < today) -> "please upload the renewed document"
  - unreadable / blurry -> rejected at once ("photo not taken properly,
    please upload a scanned PDF"); the document stays missing
  - the wrong document in a section, or an expired document -> rejected
  - typed date vs date read from the document: equal -> accepted; the
    document's own date wins when it was read by the rules; if it was read
    by the vision model and disagrees, it goes to the review queue
  - contact edits never overwrite calling-sheet data; they're queued
"""
import base64
import hashlib
import html
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from ..compliance import DOC_LABELS, evaluate, refresh_category, slab_label, sub_slab_label
from ..config import FEATURE_MESSAGING_ANALYTICS, MAX_UPLOAD_SIZE_BYTES, local_now
from ..db import SessionLocal, get_db
from ..document_service import store_extracted_document
from ..models import (AgreementEvent, ContactChangeRequest, CSP, CspQuestion, Document, DocumentStatus, InternalUser,
                      ManualReviewQueue, OutboundMessage, OutboundStatus, ReviewStatus)
from .. import master_link, pages, vault
from ..ocr_service import detect_mime, photo_problem
from ..portal_tokens import issue_upload_link, resolve_token, revoke_if_complete
from ..validation import normalize_csp_code

logger = logging.getLogger(__name__)
router = APIRouter()

PAGE = Path(__file__).resolve().parent.parent / "web" / "portal.html"
GET_LINK_PAGE = PAGE.parent / "get_link.html"
DC_PAGE = PAGE.parent / "dc_master.html"
# Inlined: only the upload routes are public, so the page can't fetch /static.
LOGO = "data:image/png;base64," + base64.b64encode(
    (PAGE.parent / "static" / "logo.png").read_bytes()).decode()
SECTIONS = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}
# Most lenient validity per type, for rejecting an obviously expired typed
# date before OCR. The document's own validity is applied after reading it.
MAX_VALIDITY_YEARS = {"AGREEMENT": 3, "POLICE_VERIFICATION": 1, "IIBF_CERTIFICATE": None}

MSG = {
    "bad_link": ("यह लिंक अब नहीं चल रहा है। नए लिंक के लिए अपने RM से बात करें।",
                 "This link is invalid or has expired. Please contact your RM for a new link."),
    "file_type": ("सिर्फ़ PDF, JPG या PNG फ़ाइल अपलोड करें।", "Please upload a PDF, JPG or PNG file only."),
    "too_many_pages": ("एक डॉक्यूमेंट में ज़्यादा से ज़्यादा 15 पेज हो सकते हैं।",
                       "A document can have at most 15 pages."),
    "pdf_or_photos": ("या तो एक PDF चुनें, या पेजों की फोटो। दोनों एक साथ नहीं।",
                      "Choose either one PDF or photos of the pages, not both."),
    "too_big": ("फ़ाइल बहुत बड़ी है।", "The file is too large."),
    "no_date": ("कृपया डॉक्यूमेंट बनने की तारीख (issue date) भरें।", "Please enter the document's issue date."),
    "future": ("यह तारीख आज के बाद की नहीं हो सकती।", "The issue date cannot be in the future."),
    "expired": ("इस डॉक्यूमेंट की तारीख निकल चुकी है (expired)। कृपया इसे रिन्यू करवाकर नया डॉक्यूमेंट अपलोड करें।",
                "This document has expired. Please renew it and upload the new document."),
    "unreadable": ("फोटो साफ़ नहीं है — कृपया स्कैन की हुई PDF अपलोड करें (Google Drive → Scan या Adobe Scan)।",
                   "Photo not taken properly — please upload a scanned PDF (Google Drive → Scan, or Adobe Scan)."),
    "wrong_doc": ("यह सही डॉक्यूमेंट नहीं है। कृपया यहाँ सही डॉक्यूमेंट अपलोड करें।",
                  "This is not the right document for this section. Please upload the correct document."),
    "duplicate": ("यह फ़ाइल पहले से हमारे पास है।", "We already have this file."),
    "application_only": ("यह पुलिस प्रमाण पत्र का आवेदन या उसकी रसीद है, प्रमाण पत्र नहीं। पुलिस से प्रमाण पत्र मिलने के बाद उसे यहाँ अपलोड करें।",
                         "This is the application or receipt for the police certificate, not the certificate itself. "
                         "Upload the certificate once the police issue it."),
    # DC master link (app/master_link.py)
    "dc_unknown_csp": ("यह KO कोड सक्रिय CSP सूची में नहीं है। कोड जाँचें।",
                       "This KO code is not in the list of active CSPs. Please check the code."),
    "dc_valid": ("यह डॉक्यूमेंट पहले से हमारे पास है और अभी मान्य है।", "Already on file and still valid."),
    "dc_in_review": ("यह डॉक्यूमेंट अभी जाँच में है।", "This document is being checked by our team."),
    "dup_other": ("यह फ़ाइल किसी दूसरे CSP के रिकॉर्ड में पहले से है। इसी CSP का अपना डॉक्यूमेंट अपलोड करें।",
                  "This file is already on record for another CSP. Upload this CSP's own document."),
    "dup_same": ("यह वही डॉक्यूमेंट है जो पहले से हमारे पास है। रिन्यू किया हुआ नया डॉक्यूमेंट अपलोड करें।",
                 "This is the same document we already have. Please upload the renewed one."),
    "iibf_receipt": ("यह IIBF परीक्षा की फीस की रसीद (या एडमिट कार्ड) है, IIBF सर्टिफिकेट नहीं। कृपया पास होने के बाद मिला IIBF सर्टिफिकेट अपलोड करें।",
                     "This is the IIBF exam fee receipt (or admit letter), not the IIBF certificate. "
                     "Please upload the certificate you received after passing."),
    # Photo checks (app/ocr_service.py: photo_problem): tell the CSP exactly what to fix.
    "too_small": ("फोटो बहुत छोटी है। पूरा पन्ना फ्रेम में रखें और कैमरे से सीधे फोटो लें (स्क्रीनशॉट या फॉरवर्ड की हुई फोटो नहीं)।",
                  "The photo is too small. Keep the whole page in the frame and take the photo with the camera "
                  "(not a screenshot or a forwarded photo)."),
    "too_dark": ("फोटो बहुत अंधेरी है। दिन की रोशनी में या बल्ब के पास, बिना परछाईं के फोटो लें।",
                 "The photo is too dark. Take it in daylight or under a light, without shadows."),
    "washed_out": ("फोटो में अक्षर दिखाई नहीं दे रहे (बहुत ज़्यादा रोशनी या फ्लैश)। फ्लैश बंद करके दोबारा फोटो लें।",
                   "The text is not visible (too much light or flash). Turn the flash off and take the photo again."),
    "blurry": ("फोटो धुंधली है। फोन को स्थिर रखें, अक्षरों पर टैप करके फोकस करें, फिर फोटो लें।",
               "The photo is blurry. Hold the phone still, tap on the text to focus, then take the photo."),
    "link_requested": ("अगर KO कोड और मोबाइल नंबर हमारे रिकॉर्ड से मिलते हैं, तो कुछ मिनट में उसी नंबर पर WhatsApp से "
                       "नया लिंक आ जाएगा। न आए तो अपने RM से बात करें।",
                       "If the KO code and mobile number match our records, a new link will reach that number on "
                       "WhatsApp in a few minutes. If it doesn't, please talk to your RM."),
    "question_sent": ("आपका सवाल आपके RM तक पहुँच गया है। वो जल्दी आपसे संपर्क करेंगे।",
                      "Your question has reached your RM. They will contact you soon."),
    "question_empty": ("कृपया अपना सवाल लिखें।", "Please write your question."),
    "too_many": ("बहुत बार कोशिश हो चुकी है। कृपया थोड़ी देर बाद फिर कोशिश करें या अपने RM से बात करें।",
                 "Too many attempts. Please try again later or talk to your RM."),
    "accepted": ("डॉक्यूमेंट मिल गया और ठीक है। धन्यवाद!", "Document accepted. Thank you!"),
    "received_check": ("डॉक्यूमेंट मिल गया। हमारी टीम इसे जाँचेगी, 1-2 दिन में आपका रिकॉर्ड अपडेट हो जाएगा।",
                       "Document received. Our team will check it and update your record in 1-2 days."),
    "review": ("डॉक्यूमेंट मिल गया। आपकी भरी हुई तारीख और डॉक्यूमेंट पर लिखी तारीख अलग है, हमारी टीम इसे चेक करेगी।",
               "Document received. The date you entered differs from the document, so our team will check it."),
}


def _msg(key: str) -> dict:
    hi, en = MSG[key]
    return {"hi": hi, "en": en}


def _staff(db: Session, uid: Optional[int]) -> Optional[InternalUser]:
    return db.get(InternalUser, uid) if uid else None


def _last_problems(db: Session, csp_id: int) -> dict:
    """Per section, the reason the CSP's latest upload of it was turned
    away (only when that latest upload was not accepted)."""
    from ..comms.upload_receipt import SHORT_REASON
    out, seen = {}, set()
    for e in (db.query(AgreementEvent).filter(AgreementEvent.csp_id == csp_id,
                                              AgreementEvent.event_type == "PORTAL_UPLOAD")
              .order_by(AgreementEvent.sent_at.desc()).limit(20)):
        for r in (e.payload or {}).get("results") or []:
            sec = r.get("section")
            if sec in seen:
                continue
            seen.add(sec)
            if not r.get("ok") and not r.get("rechecked", "").startswith("->"):
                hi, en = SHORT_REASON.get(r.get("reason"), SHORT_REASON["wrong_doc"])
                out[sec] = {"hi": hi, "en": en, "at": e.sent_at.strftime("%d-%m-%Y") if e.sent_at else ""}
    return out


def _context(db: Session, csp: CSP, requested: list[str]) -> dict:
    state = evaluate(db, csp)
    rm, dc = _staff(db, csp.rm_id), _staff(db, csp.dc_id)
    problems = _last_problems(db, csp.id)
    docs = []
    for key, t in SECTIONS.items():
        s = state.docs[t]
        en, hi = DOC_LABELS[t]
        docs.append({"section": key, "type": t, "label_en": en, "label_hi": hi, "status": s.shown_status,
                     "issue_date": s.issue_date.isoformat() if s.issue_date else None,
                     "expiry_date": s.expiry_date.isoformat() if s.expiry_date else None,
                     "requested": t in requested or (s.status != "VALID" and not s.in_review),
                     "last_problem": problems.get(key) if s.status != "VALID" and not s.in_review else None,
                     "max_validity_years": MAX_VALIDITY_YEARS[t]})
    return {"csp": {"code": csp.current_code, "name": csp.name, "email": csp.email or "",
                    "mobile": csp.phone or "", "rm": rm.name if rm else "", "dc": dc.name if dc else ""},
            "docs": docs, "today": date.today().isoformat(),
            "max_upload_mb": round(MAX_UPLOAD_SIZE_BYTES / 1024 / 1024)}


def _dc_context(db: Session, csp: CSP) -> dict:
    """The CSP's page as the DC master link shows it: the same details, plus
    which documents are locked (valid for more than 60 days, or being
    checked) and why."""
    ctx = _context(db, csp, [])
    state = evaluate(db, csp)
    locks = master_link.locks(state)
    for d in ctx["docs"]:
        lock = locks.get(d["type"])
        d["days_left"] = state.docs[d["type"]].days_left
        d["locked"] = bool(lock)
        d["lock_message"] = master_link.lock_message(lock) if lock else None
        d["requested"] = not lock
    ctx["csp"].update(label=slab_label(csp.category) if csp.category else "", sub_label=sub_slab_label(csp.sub_slab),
                      state=csp.state or "")
    ctx["nothing_needed"] = all(lock is not None and lock["key"] == "dc_valid" for lock in locks.values())
    return ctx


def _dc_list(db: Session) -> list[dict]:
    staff = {u.id: u.name for u in db.query(InternalUser.id, InternalUser.name)}
    return [{"code": c.current_code, "name": c.name, "dc": staff.get(c.dc_id) or "", "rm": staff.get(c.rm_id) or "",
             "state": c.state or "", "label": c.category}
            for c in master_link.eligible(db).order_by(CSP.current_code)]


def _dc_page(token: str, db: Session) -> HTMLResponse:
    rows = _dc_list(db)
    blob = json.dumps({"csps": rows, "dcs": sorted({r["dc"] for r in rows if r["dc"]}),
                       "today": date.today().isoformat(),
                       "max_upload_mb": round(MAX_UPLOAD_SIZE_BYTES / 1024 / 1024)},
                      ensure_ascii=False).replace("<", "\\u003c")
    return HTMLResponse(DC_PAGE.read_text(encoding="utf-8").replace("__LOGO__", LOGO).replace("__CONTEXT_JSON__", blob))


def _dc_report(db: Session, dc_name: str):
    """Live Excel of the CSPs on the master link (one DC's, or all)."""
    from fastapi.responses import StreamingResponse
    from .. import reports
    staff = {u.name.strip().lower(): u.id for u in db.query(InternalUser).filter(InternalUser.role == "DC")}
    q = master_link.eligible(db)
    if dc_name.strip():
        q = q.filter(CSP.dc_id == staff.get(dc_name.strip().lower(), -1))
    ids = [c.id for c in q]
    name = f"CSPs_{(dc_name.strip() or 'all').replace(' ', '_')}_{date.today().isoformat()}.xlsx"
    return StreamingResponse(reports.csp_report(db, csp_ids=ids),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _on_record_elsewhere(db: Session, csp: CSP, data: bytes) -> bool:
    sha = hashlib.sha256(data).hexdigest()
    return db.query(Document.id).filter(Document.sha256 == sha, Document.csp_id != csp.id).first() is not None


def _same_as_on_file(db: Session, csp: CSP, doc_type: str, ex: dict) -> bool:
    """The document read is the one already on file (same issue date)."""
    s = evaluate(db, csp).docs[doc_type]
    read = _parse_date(ex.get("start_date"))
    return bool(s.document is not None and s.status == "VALID" and read and s.document.issue_date == read)


def _list_for_review(db: Session, doc, section: str) -> None:
    """Every document stored through the master link appears in the Review
    queue for the daily report (it already counts if it was clear)."""
    label = DOC_LABELS[SECTIONS[section]][0]
    pending = (db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == doc.id,
                                                  ManualReviewQueue.status == ReviewStatus.PENDING).first())
    if pending is not None:
        if not (pending.reason or "").startswith(master_link.REVIEW_PREFIX):
            pending.reason = f"{master_link.REVIEW_PREFIX} via the master link ({label}). {pending.reason}"[:500]
        return
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
        f"{master_link.REVIEW_PREFIX} via the master link ({label}): already counted. Check it and accept, "
        "or reject it if it is wrong.")))


def _get_link_page(notice: str = "", result: str = "") -> HTMLResponse:
    def card(cls: str, key: str) -> str:
        hi, en = MSG[key]
        return f'<div class="card {cls}"><p>{html.escape(hi)}</p><p class="en">{html.escape(en)}</p></div>'
    return HTMLResponse(GET_LINK_PAGE.read_text(encoding="utf-8").replace("__LOGO__", LOGO)
                        .replace("__NOTICE__", card("notice", notice) if notice else "")
                        .replace("__RESULT__", card("done", result) if result else ""))


def _record_link_open(db: Session, csp_id: int) -> None:
    """For the messaging analytics: the CSP opened their link (at most one
    record per CSP per hour, so reloading the page isn't counted again)."""
    if not FEATURE_MESSAGING_ANALYTICS:                    # parked feature: record nothing
        return
    since = local_now() - timedelta(hours=1)               # sent_at is in local time (database clock)
    if db.query(AgreementEvent.id).filter(AgreementEvent.csp_id == csp_id, AgreementEvent.event_type == "LINK_OPENED",
                                          AgreementEvent.sent_at >= since).first():
        return
    db.add(AgreementEvent(csp_id=csp_id, event_type="LINK_OPENED", source="CSP_UPLOAD_PORTAL", channel="WEB"))
    db.commit()


@router.get("/upload", response_class=HTMLResponse)
def upload_page(token: str = Query(""), dc: str = Query(""), db: Session = Depends(get_db)):
    if dc:
        # The DC master link (app/master_link.py).
        if master_link.resolve(db, dc) is None:
            return _get_link_page(notice="bad_link")
        return _dc_page(dc, db)
    row = resolve_token(db, token)
    if row is None:
        # No token (someone typed the address) or a dead one: let the CSP ask
        # for a fresh link instead of a dead end.
        return _get_link_page(notice="bad_link" if token else "")
    csp = db.get(CSP, row.csp_id)
    _record_link_open(db, csp.id)
    ctx = _context(db, csp, row.requested_types or [])
    # JSON inside <script type="application/json">: escape "<" so a name
    # like "</script>" can't break out of the tag.
    blob = json.dumps(ctx, ensure_ascii=False).replace("<", "\\u003c")
    return HTMLResponse(PAGE.read_text(encoding="utf-8").replace("__LOGO__", LOGO).replace("__CONTEXT_JSON__", blob))


# Self-service link requests: per web address and per CSP, so the page can't
# be used to flood anyone with messages.
LINK_REQUESTS_PER_IP_HOUR = 10
LINK_SENDS_PER_CSP_DAY = 3
_ip_hits: dict[str, deque] = defaultdict(deque)
_ip_lock = threading.Lock()


def _ip_allowed(ip: str) -> bool:
    now = time.monotonic()
    with _ip_lock:
        q = _ip_hits[ip]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= LINK_REQUESTS_PER_IP_HOUR:
            return False
        q.append(now)
        return True


def _send_link(csp_id: int, phone10: str) -> None:
    """Draft and send the LINK_REQUEST message (background, own session).
    The CSP asked for it, so it skips review; the recipient guard in
    outbound.send still checks the number against the calling sheet."""
    from ..comms import outbound
    db = SessionLocal()
    try:
        csp = db.get(CSP, csp_id)
        state = evaluate(db, csp)
        link = issue_upload_link(db, csp, state.needs_upload)
        rm = _staff(db, csp.rm_id)
        ctx = {"csp_name": csp.name, "csp_code": csp.current_code, "docs": [], "upload_link": link,
               "rm_name": rm.name if rm else None}
        [m] = outbound.draft(db, csp=csp, role="CSP", template_key="LINK_REQUEST", ctx=ctx,
                             key_base=f"LINK:{csp.id}:{uuid.uuid4().hex[:12]}", channels=("WHATSAPP",),
                             stage="LINK_REQUEST")
        if m.status != OutboundStatus.BLOCKED:
            m.destination = phone10
            m.status, m.reviewed_by, m.reviewed_at = OutboundStatus.APPROVED, "CSP asked on the upload page", outbound._now()
            outbound.send(db, m)
        db.commit()
        logger.info("link_request_sent csp_id=%s status=%s", csp_id, m.status.value)
    except Exception:
        db.rollback()
        logger.exception("link_request_failed csp_id=%s", csp_id)
    finally:
        db.close()


def _send_upload_status(csp_id: int, template_name: str = "", payload_data: Optional[dict] = None,
                        results: Optional[list] = None, session_factory=None) -> None:
    """Reply to the CSP right after their documents were handled: "all
    recorded", or what was recorded / is being checked / was not (and why).
    Kept under this name for callers elsewhere (app/api/review.py)."""
    from ..comms.upload_receipt import send_upload_receipt
    send_upload_receipt(csp_id, results, session_factory=session_factory)


QUESTION_CATEGORIES = {"AGREEMENT", "UPLOAD_PROBLEM", "OTHER"}
QUESTIONS_PER_CSP_DAY = 5


def _ask_question(db: Session, token: str, category: str, text: str, phone: str) -> JSONResponse:
    row = resolve_token(db, token)
    if row is None:
        return JSONResponse({"ok": False, "message": _msg("bad_link")}, status_code=403)
    text = (text or "").strip()[:1000]
    if len(text) < 3:
        return JSONResponse({"ok": False, "message": _msg("question_empty")}, status_code=422)
    csp = db.get(CSP, row.csp_id)
    since = local_now() - timedelta(days=1)                # created_at is in local time (database clock)
    if db.query(CspQuestion).filter(CspQuestion.csp_id == csp.id, CspQuestion.created_at >= since).count() \
            >= QUESTIONS_PER_CSP_DAY:
        return JSONResponse({"ok": False, "message": _msg("too_many")}, status_code=429)
    from ..comms.outbound import _phone10
    db.add(CspQuestion(csp_id=csp.id, rm_id=csp.rm_id, text=text, callback_phone=_phone10(phone),
                       category=category if category in QUESTION_CATEGORIES else "OTHER", status="OPEN"))
    db.commit()
    return JSONResponse({"ok": True, "message": _msg("question_sent")})


@router.post("/upload", response_class=HTMLResponse)
def request_link(request: Request, background: BackgroundTasks, code: str = Form(""), mobile: str = Form(""),
                 action: str = Form(""), token: str = Form(""), category: str = Form(""), text: str = Form(""),
                 dc: str = Form(""), dc_name: str = Form(""), db: Session = Depends(get_db)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    if action in ("dc_context", "dc_report"):
        # The DC master link: only the upload routes are public, so it uses this one.
        if master_link.resolve(db, dc) is None:
            return JSONResponse({"ok": False, "message": _msg("bad_link")}, status_code=403)
        if action == "dc_report":
            return _dc_report(db, dc_name)
        csp = master_link.find(db, code)
        if csp is None:
            return JSONResponse({"ok": False, "message": _msg("dc_unknown_csp")}, status_code=404)
        return JSONResponse({"ok": True, "context": _dc_context(db, csp)})
    if action == "question":
        # Only Nginx's public routes reach the app, so questions share this one.
        if not _ip_allowed(ip):
            return JSONResponse({"ok": False, "message": _msg("too_many")}, status_code=429)
        return _ask_question(db, token, category, text, mobile)
    if not _ip_allowed(ip):
        return _get_link_page(result="too_many")
    from ..comms.outbound import _phone10, allowed_destinations
    want = normalize_csp_code(code.strip().upper()[:12])
    phone = _phone10(mobile)
    csp = (db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True),
                                (CSP.current_code == want) | (CSP.lookup_code == want)).first()
           if want and phone else None)
    matched = bool(csp and phone in allowed_destinations(db, csp, "CSP", "WHATSAPP"))
    sent_today = 0
    if matched:
        sent_today = db.query(OutboundMessage).filter(
            OutboundMessage.csp_id == csp.id, OutboundMessage.template_name == "LINK_REQUEST",
            OutboundMessage.created_at >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)).count()
        if sent_today < LINK_SENDS_PER_CSP_DAY:
            background.add_task(_send_link, csp.id, phone)
    db.add(AgreementEvent(csp_id=csp.id if csp else None, event_type="LINK_REQUEST", source="CSP_UPLOAD_PORTAL",
                          channel="WEB", payload={"code": want or code[:12], "mobile_last4": (phone or "")[-4:],
                                                  "matched": matched, "limited": sent_today >= LINK_SENDS_PER_CSP_DAY}))
    db.commit()
    # Same answer whatever happened: the page never reveals who is on record.
    return _get_link_page(result="link_requested")


@router.get("/api/portal/context")
def portal_context(token: str = Query(""), db: Session = Depends(get_db)):
    row = resolve_token(db, token)
    if row is None:
        raise HTTPException(403, detail=_msg("bad_link"))
    return _context(db, db.get(CSP, row.csp_id), row.requested_types or [])


async def _read_limited(upload: UploadFile) -> Optional[bytes]:
    chunks, total = [], 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_UPLOAD_SIZE_BYTES:
            return None
        chunks.append(chunk)


def _parse_date(v: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat((v or "").strip()[:10])
    except ValueError:
        return None


def _typed_date_problem(doc_type: str, typed: Optional[date], today: date) -> Optional[str]:
    if typed is None:
        return "no_date"
    if typed > today:
        return "future"
    # An old typed date is not turned away here: the document may state a
    # longer validity (a 5-year agreement, a lifetime police clearance).
    # Reading it decides, and "expired" is said only when certain.
    return None


def _surely_expired(ex: dict, typed: Optional[date]) -> bool:
    """Turn an upload away as expired only when nothing about it is a guess:
    the rules (not the model) read the issue date, the CSP typed the same
    date, and the validity was printed or stated (not a default)."""
    rule = (ex.get("validity_rule_used") or "").upper()
    # A police certificate that states no validity gets 1 year by Eko's
    # policy: that is a rule, not a guess. An agreement's default is a guess.
    guessed_validity = "DEFAULT" in rule and not rule.startswith("PVR_DEFAULT")
    return (not ex.get("type_review")
            and not (ex.get("date_source") or "").startswith("MODEL_VISION")
            and not guessed_validity
            and typed is not None and ex.get("start_date") == typed.isoformat())


def _record_contact_changes(db: Session, csp: CSP, form: dict) -> int:
    rm, dc = _staff(db, csp.rm_id), _staff(db, csp.dc_id)
    current = {"name": csp.name, "email": csp.email, "mobile": csp.phone,
               "rm": rm.name if rm else None, "dc": dc.name if dc else None}
    n = 0
    for field, old in current.items():
        new = (form.get(field) or "").strip()[:200]
        if new and new.lower() != (old or "").strip().lower():
            db.add(ContactChangeRequest(csp_id=csp.id, field=field, old_value=old, new_value=new))
            n += 1
    return n


@router.post("/api/portal/upload")
async def portal_upload(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    form = await request.form()
    via_dc = bool(form.get("dc"))
    if via_dc:
        # The DC master link: any working CSP, chosen by KO code.
        if master_link.resolve(db, str(form.get("dc") or "")) is None:
            raise HTTPException(403, detail=_msg("bad_link"))
        csp = master_link.find(db, str(form.get("code") or ""))
        if csp is None:
            raise HTTPException(404, detail=_msg("dc_unknown_csp"))
        row = None
    else:
        row = resolve_token(db, str(form.get("token") or ""))
        if row is None:
            raise HTTPException(403, detail=_msg("bad_link"))
        csp = db.get(CSP, row.csp_id)
    channel = master_link.CHANNEL if via_dc else "CSP_UPLOAD_PORTAL"
    today = date.today()
    # Valid for more than 60 days, or being checked: the DC may not upload it again.
    dc_locks = master_link.locks(evaluate(db, csp)) if via_dc else {}

    for field in ("name", "mobile"):
        if not via_dc and not str(form.get(field) or "").strip():
            raise HTTPException(422, detail={"hi": "नाम और मोबाइल नंबर ज़रूरी हैं।",
                                              "en": "Name and mobile number are required."})

    results = []
    for section, doc_type in SECTIONS.items():
        # One PDF, or one photo per page (several files under the same name).
        uploads = [u for u in form.getlist(f"{section}_file")
                   if (isinstance(u, UploadFile) or hasattr(u, "read")) and getattr(u, "filename", "")]
        if not uploads:
            continue
        upload = uploads[0]
        res = {"section": section, "ok": False, "filename": str(upload.filename)[:200],
               "pages": len(uploads)}
        results.append(res)

        def turn_away(reason: str, data: Optional[bytes] = None, mime: Optional[str] = None) -> None:
            res["message"], res["reason"] = _msg(reason), reason
            if data and mime:
                # Kept so the team can see what the CSP tried (dashboard: Form uploads).
                try:
                    res["kept_file"] = vault.keep_rejected_upload(data, csp.current_code, section, mime)
                except OSError:
                    logger.exception("portal_rejected_file_not_kept csp_id=%s", csp.id)

        lock = dc_locks.get(doc_type)
        if lock:
            turn_away(lock["key"])
            res["message"] = master_link.lock_message(lock)
            continue
        typed = _parse_date(form.get(f"{section}_issue_date"))
        problem = _typed_date_problem(doc_type, typed, today)
        if problem:
            turn_away(problem)
            continue
        if len(uploads) > pages.MAX_PAGES:
            turn_away("too_many_pages")
            continue
        files = []
        for u in uploads:
            files.append(await _read_limited(u))
        if any(f is None for f in files) or sum(len(f) for f in files) > MAX_UPLOAD_SIZE_BYTES:
            turn_away("too_big")
            continue
        mimes = [detect_mime(f) for f in files]
        if None in mimes:
            turn_away("file_type")
            continue
        if len(files) > 1 and "application/pdf" in mimes:
            turn_away("pdf_or_photos")
            continue
        if mimes[0] == "application/pdf":
            data, mime = files[0], mimes[0]
        else:
            bad = next(((i, p) for i, p in enumerate(photo_problem(f) for f in files) if p), None)
            if bad:
                # Not filed as a document: the CSP retakes that page right away.
                turn_away(bad[1], files[bad[0]], mimes[bad[0]])
                if len(files) > 1:
                    res["message"] = {k: f"{'पेज' if k == 'hi' else 'Page'} {bad[0] + 1}: {v}"
                                      for k, v in res["message"].items()}
                continue
            # Photos -> one small PDF (shrunk, upright, pages in order).
            data = await run_in_threadpool(lambda: pages.photos_to_pdf([pages.shrink_photo(f) for f in files]))
            mime = "application/pdf"
        if via_dc and _on_record_elsewhere(db, csp, data):
            turn_away("dup_other", data, mime)
            continue
        # The section is a hint: a readable file whose wording or date the
        # rules don't recognise goes to a person instead of being turned away.
        ex = await run_in_threadpool(lambda: extract_document_fields_deterministic(
            data, upload.filename, expected_type=doc_type, typed_issue_date=typed))
        if ex["readability"] == "UNREADABLE":
            # OCR could not read it (a faint scan, handwriting): a person
            # looks before the CSP is told it is unreadable. (Blurry, dark or
            # tiny photos were already sent back above, with what to fix.)
            if ex.get("page_count", 0) > 0:
                ex["readability"] = "READABLE"
                ex["document_type"] = doc_type
                ex["type_review"] = (f"OCR could not read this {doc_type.replace('_', ' ').lower()} "
                                     f"({ex.get('readability_reason') or 'no clear text'}). Check it, enter the "
                                     "issue date, or reject it if it really is unreadable.")
            else:
                ex["document_type"] = ex.get("document_type") if ex.get("document_type") != "UNKNOWN" else doc_type
                kept = store_extracted_document(db, csp, data, upload.filename, mime, ex, channel=channel)
                turn_away("unreadable")
                res["document_id"] = kept.document.id if kept.document is not None else None
                continue

        # If it is explicitly an unauthorized document (Aadhaar, PAN, etc.)
        if ex.get("document_type") == "REJECTED_UNAUTHORIZED":
            turn_away("wrong_doc", data, mime)
            res["read_as"] = "UNAUTHORIZED"
            continue

        # The wording says it is an application receipt (or a KYC form), not
        # the certificate: turned away whatever section it was uploaded in.
        if ex.get("document_type") in ("PVR_APPLICATION_ONLY", "NOT_A_PVR", "IIBF_RECEIPT_ONLY"):
            turn_away({"PVR_APPLICATION_ONLY": "application_only", "IIBF_RECEIPT_ONLY": "iibf_receipt"}
                      .get(ex["document_type"], "wrong_doc"), data, mime)
            res["read_as"] = ex["document_type"]
            continue

        # Reads as another required document (an IIBF certificate uploaded in
        # the police box): filed as what it reads as, and a person confirms
        # before it counts. The CSP is not told "wrong document": the reading
        # may be wrong, and if it is right the document is still needed.
        if ex.get("document_type") in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE") and ex["document_type"] != doc_type:
            res["read_as"] = ex["document_type"]
            ex["type_review"] = " ".join(x for x in (ex.get("type_review"), (
                f"Uploaded in the {doc_type.replace('_', ' ').lower()} box but reads as a "
                f"{ex['document_type'].replace('_', ' ').lower()}; check which document it is "
                "(change the type if needed).")) if x)

        # If document_type was UNKNOWN, fall back to human review for the expected section
        if ex.get("document_type") not in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE"):
            ex["document_type"] = doc_type
            ex["type_review"] = (ex.get("type_review") or "") + f" Document wording in {doc_type} section needs human verification."

        if ex.get("compliance_status") == "EXPIRED":
            if _surely_expired(ex, typed):
                turn_away("expired", data, mime)
                continue
            # Not certain: a person checks before the CSP hears "expired".
            ex["type_review"] = " ".join(x for x in (ex.get("type_review"), (
                f"Read as expired on {ex.get('expiry_date')} (issued {ex.get('start_date')}, "
                f"{ex.get('validity_rule_used')}); the CSP typed issue date {typed:%d-%m-%Y}. "
                "Check the dates and the validity before the CSP is told.")) if x)

        # If OCR could not read the issue date, take the date typed by the CSP and compute expiry
        if typed and not ex.get("start_date"):
            ex["start_date"] = typed.isoformat()
            ex["date_source"] = "CSP_PORTAL_TYPED"
            from ..expiry_engine import calculate_document_expiry
            exp = calculate_document_expiry(
                doc_type=doc_type,
                issue_date=typed,
                explicit_expiry_date=_parse_date(ex.get("explicit_expiry")),
                has_explicit_3year_clause=ex.get("has_explicit_3year_clause", False),
                today=today,
                validity_months=ex.get("validity_months"),
            )
            ex["expiry_date"] = exp["calculated_expiry"].isoformat() if exp.get("calculated_expiry") else (
                "LIFETIME_NO_EXPIRY" if doc_type == "IIBF_CERTIFICATE" else None
            )
            ex["type_review"] = (ex.get("type_review") or "") + f" Issue date entered as {typed.strftime('%d-%m-%Y')} by CSP; verify on document."

        if ex.get("type_review") and typed:
            ex["type_review"] += f" The CSP typed issue date {typed.strftime('%d-%m-%Y')}."
        read_date = _parse_date(ex.get("start_date"))
        needs_review = False
        if read_date and typed != read_date:
            ex["typed_issue_date"] = typed.isoformat()
            needs_review = (ex.get("date_source") or "").startswith("MODEL_VISION")
        if via_dc and _same_as_on_file(db, csp, doc_type, ex):
            turn_away("dup_same", data, mime)
            continue
        out = store_extracted_document(db, csp, data, upload.filename, mime, ex, channel=channel,
                                       sender_on_sheet=True)
        if out.decision == "DUPLICATE":
            res.update(message=_msg("duplicate"), reason="duplicate", ok=True,
                       document_id=out.document.id if out.document is not None else None)
            continue
        doc = out.document
        if ex.get("typed_issue_date"):
            doc.extracted_fields = {**(doc.extracted_fields or {}), "typed_issue_date": ex["typed_issue_date"]}
        if needs_review:
            doc.status = DocumentStatus.NEEDS_REVIEW
            db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING,
                                     reason=f"Typed issue date {typed} differs from date read by model {read_date}."))
        if via_dc:
            db.flush()
            _list_for_review(db, doc, section)
        said = "received_check" if ex.get("type_review") else ("review" if needs_review else "accepted")
        res.update(ok=True, message=_msg(said), reason=said, document_id=doc.id,
                   issue_date=ex.get("start_date"), expiry_date=ex.get("expiry_date"))

    if not results:
        raise HTTPException(422, detail={"hi": "कम से कम एक डॉक्यूमेंट चुनें।", "en": "Please choose at least one document."})

    changes = 0 if via_dc else _record_contact_changes(db, csp, {k: form.get(k) for k in ("name", "email", "mobile", "rm", "dc")})
    slab_before = csp.category
    # Category, and the follow-up cycles this upload satisfies, update now
    # (queued reminders for them are cancelled), not at the next daily run.
    from ..renewal_engine import on_documents_received
    state = on_documents_received(db, csp, today)
    # One row per submission: the "Form uploads" page reads these.
    keep = ("section", "ok", "reason", "filename", "document_id", "kept_file", "read_as", "issue_date", "expiry_date")
    db.add(AgreementEvent(csp_id=csp.id, event_type="PORTAL_UPLOAD", source=channel, channel="WEB",
                          payload={"results": [{k: r.get(k) for k in keep if r.get(k) is not None} for r in results],
                                   "contact_changes": changes, "slab_before": slab_before,
                                   "slab_after": state.category, "sub_slab_after": csp.sub_slab,
                                   "still_needed": state.needs_upload}))
    if row is not None:
        revoke_if_complete(db, row, state.needs_upload)
    db.commit()

    # Reply to the CSP at once: what was recorded and what wasn't (and why).
    # Same database as this request (its own session: it runs after the reply).
    from sqlalchemy.orm import sessionmaker
    background.add_task(_send_upload_status, csp.id, results=[{k: r.get(k) for k in ("section", "ok", "reason")}
                                                             for r in results],
                        session_factory=sessionmaker(bind=db.get_bind()))

    shown = ("section", "ok", "message", "issue_date", "expiry_date")       # nothing internal to the browser
    return {"results": [{k: r[k] for k in shown if k in r} for r in results], "category": state.category,
            "still_needed": [DOC_LABELS[t][0] for t in state.needs_upload]}
