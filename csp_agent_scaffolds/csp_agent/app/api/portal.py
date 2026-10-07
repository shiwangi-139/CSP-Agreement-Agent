"""
app/api/portal.py
Mobile upload portal for CSPs (Hindi + English), reached from the link in
the WhatsApp / email messages.

GET  /upload?token=...          the page (app/web/portal.html); with no token
                                or a dead one: "get your upload link" (app/web/get_link.html)
POST /upload                    KO code + mobile -> a fresh link by WhatsApp, only to
                                a number on the calling sheet (never shown on screen)
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
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from ..compliance import DOC_LABELS, evaluate, refresh_category
from ..config import MAX_UPLOAD_SIZE_BYTES
from ..db import SessionLocal, get_db
from ..document_service import store_extracted_document
from ..expiry_engine import add_years
from ..models import (AgreementEvent, ContactChangeRequest, CSP, DocumentStatus, InternalUser,
                      ManualReviewQueue, OutboundMessage, OutboundStatus, ReviewStatus)
from ..ocr_service import detect_mime, photo_problem
from ..portal_tokens import issue_upload_link, resolve_token, revoke_if_complete
from ..validation import normalize_csp_code

logger = logging.getLogger(__name__)
router = APIRouter()

PAGE = Path(__file__).resolve().parent.parent / "web" / "portal.html"
GET_LINK_PAGE = PAGE.parent / "get_link.html"
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
    "too_many": ("बहुत बार कोशिश हो चुकी है। कृपया थोड़ी देर बाद फिर कोशिश करें या अपने RM से बात करें।",
                 "Too many attempts. Please try again later or talk to your RM."),
    "accepted": ("डॉक्यूमेंट मिल गया और ठीक है। धन्यवाद!", "Document accepted. Thank you!"),
    "review": ("डॉक्यूमेंट मिल गया। आपकी भरी हुई तारीख और डॉक्यूमेंट पर लिखी तारीख अलग है, हमारी टीम इसे चेक करेगी।",
               "Document received. The date you entered differs from the document, so our team will check it."),
}


def _msg(key: str) -> dict:
    hi, en = MSG[key]
    return {"hi": hi, "en": en}


def _staff(db: Session, uid: Optional[int]) -> Optional[InternalUser]:
    return db.get(InternalUser, uid) if uid else None


def _context(db: Session, csp: CSP, requested: list[str]) -> dict:
    state = evaluate(db, csp)
    rm, dc = _staff(db, csp.rm_id), _staff(db, csp.dc_id)
    docs = []
    for key, t in SECTIONS.items():
        s = state.docs[t]
        en, hi = DOC_LABELS[t]
        docs.append({"section": key, "type": t, "label_en": en, "label_hi": hi, "status": s.status,
                     "issue_date": s.issue_date.isoformat() if s.issue_date else None,
                     "expiry_date": s.expiry_date.isoformat() if s.expiry_date else None,
                     "requested": t in requested or s.status != "VALID",
                     "max_validity_years": MAX_VALIDITY_YEARS[t]})
    return {"csp": {"code": csp.current_code, "name": csp.name, "email": csp.email or "",
                    "mobile": csp.phone or "", "rm": rm.name if rm else "", "dc": dc.name if dc else ""},
            "docs": docs, "today": date.today().isoformat(),
            "max_upload_mb": round(MAX_UPLOAD_SIZE_BYTES / 1024 / 1024)}


def _get_link_page(notice: str = "", result: str = "") -> HTMLResponse:
    def card(cls: str, key: str) -> str:
        hi, en = MSG[key]
        return f'<div class="card {cls}"><p>{html.escape(hi)}</p><p class="en">{html.escape(en)}</p></div>'
    return HTMLResponse(GET_LINK_PAGE.read_text(encoding="utf-8").replace("__LOGO__", LOGO)
                        .replace("__NOTICE__", card("notice", notice) if notice else "")
                        .replace("__RESULT__", card("done", result) if result else ""))


@router.get("/upload", response_class=HTMLResponse)
def upload_page(token: str = Query(""), db: Session = Depends(get_db)):
    row = resolve_token(db, token)
    if row is None:
        # No token (someone typed the address) or a dead one: let the CSP ask
        # for a fresh link instead of a dead end.
        return _get_link_page(notice="bad_link" if token else "")
    csp = db.get(CSP, row.csp_id)
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
               "rm_name": rm.name if rm else None, "rm_phone": rm.phone if rm else None}
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


@router.post("/upload", response_class=HTMLResponse)
def request_link(request: Request, background: BackgroundTasks, code: str = Form(""), mobile: str = Form(""),
                 db: Session = Depends(get_db)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
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
    years = MAX_VALIDITY_YEARS[doc_type]
    if years and add_years(typed, years) < today:
        return "expired"
    return None


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
async def portal_upload(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    row = resolve_token(db, str(form.get("token") or ""))
    if row is None:
        raise HTTPException(403, detail=_msg("bad_link"))
    csp = db.get(CSP, row.csp_id)
    today = date.today()

    for field in ("name", "mobile"):
        if not str(form.get(field) or "").strip():
            raise HTTPException(422, detail={"hi": "नाम और मोबाइल नंबर ज़रूरी हैं।",
                                              "en": "Name and mobile number are required."})

    results = []
    for section, doc_type in SECTIONS.items():
        upload = form.get(f"{section}_file")
        if not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
            continue
        if not getattr(upload, "filename", ""):
            continue
        res = {"section": section, "ok": False}
        results.append(res)

        typed = _parse_date(form.get(f"{section}_issue_date"))
        problem = _typed_date_problem(doc_type, typed, today)
        if problem:
            res["message"] = _msg(problem)
            continue
        data = await _read_limited(upload)
        if data is None:
            res["message"] = _msg("too_big")
            continue
        mime = detect_mime(data)
        if mime is None:
            res["message"] = _msg("file_type")
            continue

        problem = photo_problem(data)
        if problem:
            # Not stored: the CSP retakes the photo right away.
            res["message"] = _msg(problem)
            continue
        ex = await run_in_threadpool(extract_document_fields_deterministic, data, upload.filename)
        if ex["readability"] == "UNREADABLE":
            ex["document_type"] = ex.get("document_type") if ex.get("document_type") != "UNKNOWN" else doc_type
            store_extracted_document(db, csp, data, upload.filename, mime, ex, channel="CSP_UPLOAD_PORTAL")
            res["message"] = _msg("unreadable")
            continue
        if ex["readability"] != "READABLE" or ex["document_type"] != doc_type:
            res["message"] = _msg("wrong_doc")
            continue
        if ex.get("compliance_status") == "EXPIRED":
            res["message"] = _msg("expired")
            continue

        read_date = _parse_date(ex.get("start_date"))
        needs_review = False
        if read_date and typed != read_date:
            ex["typed_issue_date"] = typed.isoformat()
            needs_review = (ex.get("date_source") or "").startswith("MODEL_VISION")
        out = store_extracted_document(db, csp, data, upload.filename, mime, ex, channel="CSP_UPLOAD_PORTAL",
                                       sender_on_sheet=True)
        if out.decision == "DUPLICATE":
            res["message"] = _msg("duplicate")
            res["ok"] = True
            continue
        doc = out.document
        if ex.get("typed_issue_date"):
            doc.extracted_fields = {**(doc.extracted_fields or {}), "typed_issue_date": ex["typed_issue_date"]}
        if needs_review:
            doc.status = DocumentStatus.NEEDS_REVIEW
            db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING,
                                     reason=f"Typed issue date {typed} differs from date read by model {read_date}."))
        res.update(ok=True, message=_msg("review" if needs_review else "accepted"),
                   issue_date=ex.get("start_date"), expiry_date=ex.get("expiry_date"))

    if not results:
        raise HTTPException(422, detail={"hi": "कम से कम एक डॉक्यूमेंट चुनें।", "en": "Please choose at least one document."})

    changes = _record_contact_changes(db, csp, {k: form.get(k) for k in ("name", "email", "mobile", "rm", "dc")})
    db.add(AgreementEvent(csp_id=csp.id, event_type="PORTAL_UPLOAD", source="CSP_UPLOAD_PORTAL", channel="WEB",
                          payload={"results": [{k: r.get(k) for k in ("section", "ok")} for r in results],
                                   "contact_changes": changes}))
    # Category, and the follow-up cycles this upload satisfies, update now
    # (queued reminders for them are cancelled), not at the next daily run.
    from ..renewal_engine import on_documents_received
    state = on_documents_received(db, csp, today)
    revoke_if_complete(db, row, state.needs_upload)
    db.commit()
    return {"results": results, "category": state.category,
            "still_needed": [DOC_LABELS[t][0] for t in state.needs_upload]}
