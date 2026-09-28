"""
app/gmail_ingest.py
Read each Gmail message ONCE, thoroughly, and record a final decision.

Backfill (first run): pages through every message with a PDF/JPG/PNG
attachment from the last GMAIL_BACKFILL_DAYS (730) days, a chunk per run,
saving the page token in ingest_state so a restart resumes where it stopped.
The mailbox historyId is captured before the backfill starts.

Afterwards: only messages added since the saved historyId (Gmail history
API) are fetched. Nothing is ever re-read: a message id already in
inbound_messages is skipped before any download, and an attachment whose
bytes are already stored for that CSP is skipped as DUPLICATE, so no OCR or
model time is spent twice.

Matching a message to a CSP (only CSPs from the calling sheet; no CSP is
ever invented from an email):
  1. "CSP Code- 1A850004" in the body
  2. "... for KO 1A850004" in the subject
  3. any CSP code in subject, body or attachment file names
  4. the sender's email address = a CSP's email on the calling sheet
  5. the CSP code printed inside an attachment
There is no sender allowlist (CSPs use personal mail); a sender who is not
on the calling sheet and not @eko.co.in is flagged on the dashboard.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import GMAIL_BACKFILL_DAYS, INTERNAL_EMAIL_DOMAIN, MAX_UPLOAD_SIZE_BYTES
from .db import SessionLocal
from .models import CSP, Document, IngestState, InboundMessage, InternalUser, EmailCategory
from .validation import normalize_csp_code
from .ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from .extract_pool import extract_all
from .ocr_service import detect_mime
from .document_service import store_extracted_document
from .compliance import refresh_category

logger = logging.getLogger(__name__)

STATE_KEY = "gmail"
CODE = r"\d[A-Z]\d{6}"
BODY_CODE = re.compile(r"CSP\s*(?:Code|ID|Id|No\.?)\s*[-:–]*\s*[\"']?(" + CODE + r")", re.I)
BODY_NAME = re.compile(r"CSP\s*Name\s*[-:–]+\s*([^\n\r]{2,60})", re.I)
SUBJECT_KO = re.compile(r"\bK\.?O\.?\b\s*(?:Code|ID)?\s*[-:#]*\s*[\"'“]?(" + CODE + r")", re.I)
ANY_CODE = re.compile(r"\b(" + CODE + r")\b")
REQUEST_KIND = [
    (re.compile(r"terminal\s+reset|reset\s+(?:of\s+)?terminal", re.I), EmailCategory.TERMINAL_RESET),
    (re.compile(r"terminal\s+extension|extension\s+(?:of\s+)?terminal|extend\s+(?:the\s+)?terminal", re.I),
     EmailCategory.TERMINAL_EXTENSION),
]
ATTACHMENT_EXTS = (".pdf", ".jpg", ".jpeg", ".png")
DOC_SUBJECT = re.compile(r"agreement|police\s*verification|\bpvr\b|character\s*certificate|\biibf\b|\bbc\s*/?\s*bf\b|"
                         r"terminal\s+(?:extension|reset)", re.I)
BASE_FILTER = "has:attachment -in:spam -in:trash"


def backfill_queries(db: Session, cutoff: str) -> list[str]:
    """Only mail that can carry CSP documents (per the business process):
    terminal extension/reset requests, mail naming one of the documents,
    and mail sent from a CSP's own address on the calling sheet."""
    base = f"{BASE_FILTER} after:{cutoff}"
    queries = [
        f"subject:terminal (subject:extension OR subject:reset) {base}",
        f'subject:(agreement OR "police verification" OR PVR OR "character certificate" OR IIBF) {base}',
    ]
    emails = sorted({e for (e,) in db.query(CSP.email).filter(CSP.email.isnot(None))})
    for i in range(0, len(emails), 40):
        queries.append("from:(" + " OR ".join(emails[i:i + 40]) + f") {base}")
    return queries


def is_relevant(db: Session, subject: str, sender_email: str) -> bool:
    if DOC_SUBJECT.search(subject or ""):
        return True
    return bool(sender_email and db.query(CSP.id).filter(CSP.email == sender_email.lower()).first())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _state(db: Session) -> IngestState:
    st = db.get(IngestState, STATE_KEY)
    if st is None:
        st = IngestState(key=STATE_KEY, value={})
        db.add(st)
        db.flush()
    return st


def _save_state(db: Session, st: IngestState, **changes) -> None:
    value = dict(st.value or {})
    value.update(changes)
    st.value = value
    st.updated_at = _now()


def _email_date(item: dict) -> Optional[datetime]:
    try:
        d = parsedate_to_datetime(item.get("date") or "")
        return d.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        ms = item.get("internal_date_ms")
        return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None) if ms else None


def _find_csp(db: Session, code: Optional[str]) -> Optional[CSP]:
    if not code:
        return None
    code = normalize_csp_code(code)
    return db.query(CSP).filter((CSP.lookup_code == code) | (CSP.current_code == code)).first()


def match_csp(db: Session, subject: str, body: str, sender_email: str, filenames: list[str]) -> tuple[Optional[CSP], str]:
    for label, pat, text in (("BODY_CSP_CODE", BODY_CODE, body), ("SUBJECT_KO", SUBJECT_KO, subject)):
        m = pat.search(text or "")
        csp = _find_csp(db, m.group(1).upper()) if m else None
        if csp:
            return csp, label
    for code in ANY_CODE.findall(f"{subject}\n{body}\n" + "\n".join(filenames).upper()):
        csp = _find_csp(db, code)
        if csp:
            return csp, "CODE_IN_TEXT"
    if sender_email:
        csp = db.query(CSP).filter(CSP.email == sender_email.lower()).first()
        if csp:
            return csp, "SENDER_EMAIL"
    return None, "NONE"


def sender_is_known(db: Session, sender_email: str) -> bool:
    e = (sender_email or "").lower()
    if not e:
        return False
    if e.endswith("@" + INTERNAL_EMAIL_DOMAIN):
        return True
    return bool(db.query(CSP.id).filter(CSP.email == e).first()
                or db.query(InternalUser.id).filter(InternalUser.email == e).first())


def _category(subject: str, body: str) -> EmailCategory:
    for pat, cat in REQUEST_KIND:
        if pat.search(subject or "") or pat.search((body or "")[:500]):
            return cat
    return EmailCategory.UNKNOWN


def process_message(db: Session, service, msg_id: str) -> str:
    """Process one Gmail message end to end. Returns the final status."""
    from .comms.gmail_oauth import get_message, fetch_attachment_bytes

    if db.query(InboundMessage.id).filter(InboundMessage.external_message_id == msg_id).first():
        return "ALREADY_PROCESSED"

    item = get_message(service, msg_id)
    sender_email = parseaddr(item["sender"])[1].lower()
    received = _email_date(item)
    if not is_relevant(db, item["subject"], sender_email):
        if db.query(InboundMessage.id).filter(InboundMessage.external_message_id == msg_id).first():
            return "ALREADY_PROCESSED"
        db.add(InboundMessage(external_message_id=msg_id, thread_id=item.get("thread_id"),
                              sender=item["sender"][:300], subject=(item["subject"] or "")[:500],
                              received_at=received, status="IGNORED_NOT_RELEVANT", processed_at=_now(),
                              sender_on_sheet=sender_is_known(db, sender_email),
                              error_message="Not a terminal request, not a document email, not from a CSP."))
        return "IGNORED_NOT_RELEVANT"
    attachments = [a for a in item["attachments"] if a["filename"].lower().endswith(ATTACHMENT_EXTS)]
    inbound = InboundMessage(
        external_message_id=msg_id, thread_id=item.get("thread_id"), sender=item["sender"][:300],
        subject=(item["subject"] or "")[:500], email_category=_category(item["subject"], item["body"]),
        received_at=received, status="PROCESSING", sender_on_sheet=sender_is_known(db, sender_email),
    )
    try:
        with db.begin_nested():
            db.add(inbound)
            db.flush()
    except IntegrityError:
        # Another scan claimed this message a moment ago.
        return "ALREADY_PROCESSED"

    csp, how = match_csp(db, item["subject"], item["body"], sender_email, [a["filename"] for a in attachments])
    decisions = []
    downloaded: dict[str, bytes] = {}
    extracted: dict[str, dict] = {}

    def fetch(att) -> Optional[bytes]:
        if att["attachment_id"] not in downloaded:
            if (att.get("size") or 0) > MAX_UPLOAD_SIZE_BYTES:
                return None
            downloaded[att["attachment_id"]] = fetch_attachment_bytes(service, msg_id, att["attachment_id"])
        return downloaded[att["attachment_id"]]

    # Download everything first (small and fast), then read the files in
    # parallel (app/extract_pool.py); saving stays in order below.
    for att in attachments:
        try:
            fetch(att)
        except Exception:
            logger.exception("attachment_download_failed msg=%s file=%s", msg_id, att["filename"])

    def read_all(atts) -> None:
        todo = [a for a in atts if downloaded.get(a["attachment_id"]) and a["attachment_id"] not in extracted]
        for a, ex in zip(todo, extract_all([(downloaded[a["attachment_id"]], a["filename"]) for a in todo])):
            extracted[a["attachment_id"]] = ex

    if csp is None:
        # Last resort: the CSP code printed inside an attachment.
        read_all(attachments)
        for att in attachments:
            ex = extracted.get(att["attachment_id"])
            csp = _find_csp(db, ex.get("csp_code")) if ex else None
            if csp:
                how = "CODE_IN_ATTACHMENT"
                break

    if csp is None:
        inbound.status = "UNMATCHED_NO_CSP"
        body_name = BODY_NAME.search(item["body"] or "")
        code = BODY_CODE.search(item["body"] or "") or SUBJECT_KO.search(item["subject"] or "")
        inbound.error_message = (f"No CSP on the calling sheet matches this email "
                                 f"(code seen: {code.group(1) if code else 'none'}, "
                                 f"name seen: {body_name.group(1).strip() if body_name else 'none'}).")[:500]
        inbound.processed_at = _now()
        return inbound.status

    inbound.csp_id = csp.id
    # The same file already on record for this CSP needs no OCR at all.
    duplicate_ids = set()
    for att in attachments:
        data = downloaded.get(att["attachment_id"])
        if data and att["attachment_id"] not in extracted and db.query(Document.id).filter(
                Document.csp_id == csp.id, Document.sha256 == hashlib.sha256(data).hexdigest()).first():
            duplicate_ids.add(att["attachment_id"])
    read_all([a for a in attachments if a["attachment_id"] not in duplicate_ids])
    for att in attachments:
        entry = {"filename": att["filename"], "decision": None, "document_id": None, "reason": None}
        try:
            data = fetch(att)
            if not data:
                entry.update(decision="NOT_ALLOWED", reason="Attachment too large or could not be downloaded.")
                decisions.append(entry)
                continue
            if att["attachment_id"] in duplicate_ids:
                ex = {"readability": "DUPLICATE"}
            else:
                ex = extracted.get(att["attachment_id"]) or extract_document_fields_deterministic(data, att["filename"])
            with db.begin_nested():
                out = store_extracted_document(
                    db, csp, data, att["filename"], detect_mime(data), ex, channel="GMAIL_INBOUND",
                    source_message_id=msg_id, source_date=received, sender_on_sheet=inbound.sender_on_sheet)
            known = out.document if out.decision == "DUPLICATE" else None
            entry.update(decision=out.decision, reason=out.reason,
                         document_id=out.document.id if out.document else None,
                         document_type=ex.get("document_type") or (known.document_type if known else None),
                         issue_date=ex.get("start_date") or (known.issue_date.isoformat() if known and known.issue_date else None),
                         expiry_date=ex.get("expiry_date") or (known.expiry_date.isoformat() if known and known.expiry_date else None))
        except Exception as e:
            logger.exception("attachment_failed msg=%s file=%s", msg_id, att["filename"])
            entry.update(decision="ERROR", reason=f"{type(e).__name__}: {e}"[:300])
        decisions.append(entry)

    inbound.attachment_decisions = decisions
    inbound.status = "PROCESSED"
    inbound.error_message = f"Matched CSP {csp.current_code} by {how}."
    inbound.processed_at = _now()
    from .renewal_engine import on_documents_received
    on_documents_received(db, csp)
    return inbound.status


def _process_ids(db: Session, service, ids: list[str], summary: dict) -> None:
    for mid in ids:
        try:
            status = process_message(db, service, mid)
            db.commit()
            summary[status] = summary.get(status, 0) + 1
        except Exception as e:
            db.rollback()
            summary["ERROR"] = summary.get("ERROR", 0) + 1
            logger.exception("gmail_message_failed id=%s: %s", mid, e)


def run_scan(max_messages: int = 200) -> dict:
    """One ingestion tick. Only one scan runs at a time (Postgres lock), no
    matter whether it was started by the worker, the dashboard or a script."""
    from .worker import advisory_lock
    with advisory_lock("gmail-scan") as got:
        if not got:
            logger.info("gmail_scan_skipped: another scan is already running")
            return {"mode": "SKIPPED", "reason": "another Gmail scan is already running"}
        return _run_scan(max_messages)


def _run_scan(max_messages: int = 200) -> dict:
    """Continue the backfill if unfinished, otherwise fetch only new mail."""
    from .comms.gmail_oauth import (get_gmail_service, list_message_ids, current_history_id,
                                    new_message_ids_since, HistoryTooOld)

    db = SessionLocal()
    summary: dict = {"mode": None}
    try:
        service = get_gmail_service()
        st = _state(db)
        state = dict(st.value or {})
        if not state.get("backfill_complete"):
            summary["mode"] = "BACKFILL"
            if not state.get("history_id"):
                cutoff = (_now() - timedelta(days=GMAIL_BACKFILL_DAYS)).strftime("%Y/%m/%d")
                _save_state(db, st, history_id=current_history_id(service), backfill_cutoff=cutoff,
                            backfill_query_index=0, backfill_started_at=_now().isoformat(), backfill_seen=0)
                db.commit()
                state = dict(st.value)
            queries = backfill_queries(db, state["backfill_cutoff"])
            done = 0
            while done < max_messages:
                qi = int(st.value.get("backfill_query_index", 0))
                if qi >= len(queries):
                    _save_state(db, st, backfill_complete=True, backfill_finished_at=_now().isoformat(), page_token=None)
                    db.commit()
                    break
                ids, token = list_message_ids(service, queries[qi], st.value.get("page_token"))
                _process_ids(db, service, ids, summary)
                done += len(ids)
                changes = {"page_token": token, "backfill_seen": int(st.value.get("backfill_seen", 0)) + len(ids),
                           "backfill_queries_total": len(queries)}
                if not token:
                    changes["backfill_query_index"] = qi + 1
                _save_state(db, st, **changes)
                db.commit()
        else:
            summary["mode"] = "INCREMENTAL"
            try:
                ids, newest = new_message_ids_since(service, state["history_id"])
            except HistoryTooOld:
                # Worker was down for over a week: re-list recent mail by date.
                # Already-processed ids are skipped, so this can't duplicate.
                since = (_now() - timedelta(days=14)).strftime("%Y/%m/%d")
                ids, token = [], None
                while True:
                    page, token = list_message_ids(service, f"{BASE_FILTER} after:{since}", token)
                    ids += page
                    if not token:
                        break
                newest = current_history_id(service)
            _process_ids(db, service, ids[:max_messages * 5], summary)
            _save_state(db, st, history_id=newest, last_incremental_at=_now().isoformat())
            db.commit()
        summary["state"] = {k: v for k, v in (st.value or {}).items() if k != "page_token"}
        return summary
    finally:
        db.close()


def ingestion_status(db: Session) -> dict:
    st = db.get(IngestState, STATE_KEY)
    value = dict(st.value) if st and st.value else {}
    value.pop("page_token", None)
    counts = dict(db.query(InboundMessage.status, __import__("sqlalchemy").func.count())
                  .group_by(InboundMessage.status).all())
    return {"state": value, "messages_by_status": counts}
