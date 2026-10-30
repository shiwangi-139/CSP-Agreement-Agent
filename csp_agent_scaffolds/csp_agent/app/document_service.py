"""
app/document_service.py
Store one extracted file for one CSP. Used by Gmail ingestion and the
upload portal so both make identical decisions.

  READABLE     -> file in the CSP folder, Document row; becomes current if
                  its issue date is the newest for that type (the older one
                  moves to expired/)
  UNREADABLE   -> file in unreadable/ for audit, Document row with status
                  UNREADABLE; that type counts as MISSING (never reviewed)

Where each file sits and what it is called is decided by app/vault.py.
  NOT_ALLOWED  -> nothing stored (it may be an Aadhaar/PAN copy)
  DUPLICATE    -> same bytes already stored for this CSP; nothing new
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from . import vault
from .compliance import USABLE, canonical_type
from .models import Agreement, CSP, Document, DocumentStatus, ManualReviewQueue, RenewalStatus, ReviewStatus

logger = logging.getLogger(__name__)


@dataclass
class StoreOutcome:
    decision: str                   # READABLE, UNREADABLE, NOT_ALLOWED, DUPLICATE
    document: Optional[Document] = None
    reason: Optional[str] = None


def _d(v) -> Optional[date]:
    if not v or v == "LIFETIME_NO_EXPIRY":
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _status_for(ex: dict) -> DocumentStatus:
    if ex.get("compliance_status") == "EXPIRED":
        return DocumentStatus.EXPIRED
    # Model-filled fields are kept, but flagged for a quick look.
    if (ex.get("date_source") or "").startswith("MODEL_VISION"):
        return DocumentStatus.NEEDS_APPROVAL
    return DocumentStatus.VALID


def sync_agreement_row(db: Session, csp: CSP) -> None:
    """Keep the legacy agreements table (used by older dashboard code) in
    step with the current documents."""
    current = {canonical_type(d.document_type): d for d in db.query(Document).filter(
        Document.csp_id == csp.id, Document.is_current.is_(True), Document.readability == "READABLE")}
    agr_doc, pvr_doc = current.get("AGREEMENT"), current.get("POLICE_VERIFICATION")
    if agr_doc is None and pvr_doc is None:
        return
    agr = db.query(Agreement).filter(Agreement.csp_id == csp.id, Agreement.is_active.is_(True)).first()
    if agr is None:
        agr = Agreement(csp_id=csp.id, is_active=True, current_csp_code=csp.current_code,
                        renewal_status=RenewalStatus.ACTIVE)
        db.add(agr)
    if agr_doc is not None:
        agr.start_date, agr.expiry_date = agr_doc.issue_date, agr_doc.expiry_date
        agr.pdf_hash, agr.pdf_link = agr_doc.sha256, agr_doc.storage_path
        expired = agr_doc.expiry_date is not None and agr_doc.expiry_date < date.today()
        agr.renewal_status = RenewalStatus.EXPIRED_LOCKED if expired else RenewalStatus.ACTIVE
    if pvr_doc is not None:
        agr.police_verification_expiry = pvr_doc.expiry_date
    db.flush()
    for d in (agr_doc, pvr_doc):
        if d is not None:
            d.agreement_id = agr.id


def same_iibf(db: Session, csp: CSP, reg_no: Optional[str], issue: Optional[date]) -> Optional[Document]:
    """The CSP's IIBF certificate already on record with this registration
    number or issue date (a CSP has only one), if any."""
    if not reg_no and issue is None:
        return None
    for d in db.query(Document).filter(Document.csp_id == csp.id, Document.document_type == "IIBF_CERTIFICATE",
                                       Document.readability == "READABLE",
                                       Document.status != DocumentStatus.REJECTED):
        if (reg_no and d.iibf_reg_number and reg_no.strip() == d.iibf_reg_number.strip()) \
                or (issue is not None and d.issue_date == issue):
            return d
    return None


def store_extracted_document(
    db: Session, csp: CSP, data: bytes, filename: str, mime_type: Optional[str],
    extraction: dict, channel: str, source_message_id: Optional[str] = None,
    source_date: Optional[datetime] = None, sender_on_sheet: Optional[bool] = None,
) -> StoreOutcome:
    decision = extraction.get("readability")
    sha = hashlib.sha256(data).hexdigest()
    if decision == "NOT_ALLOWED":
        return StoreOutcome("NOT_ALLOWED", reason=extraction.get("rejection_reason"))

    dup = db.query(Document).filter(Document.csp_id == csp.id, Document.sha256 == sha).first()
    upgrade = None
    if dup is not None:
        if dup.readability != "UNREADABLE" or decision != "READABLE":
            return StoreOutcome("DUPLICATE", dup, "Same file already on record.")
        # The same file was unreadable before and reads now (the rules have
        # improved, or the upload page sends it to a person): it is read again
        # in place, instead of being turned away on the old verdict.
        upgrade = dup

    doc_type = canonical_type(extraction.get("document_type")) or "UNKNOWN"
    issue, expiry = _d(extraction.get("start_date")), _d(extraction.get("expiry_date"))
    if doc_type == "IIBF_CERTIFICATE" and decision == "READABLE":
        same = same_iibf(db, csp, extraction.get("iibf_registration_number"), issue)
        if same is not None:
            # A person has one IIBF certificate: a new photo or scan of it is not a new document.
            return StoreOutcome("DUPLICATE", same, "Same IIBF certificate already on record (same registration no. or date).")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    arrived = (upgrade.source_date or upgrade.uploaded_at or now) if upgrade is not None else (source_date or now)

    path = upgrade.storage_path if upgrade is not None else vault.stage(data, sha, mime_type or "application/pdf")
    fields = {k: extraction.get(k) for k in (
        "start_date", "expiry_date", "explicit_expiry", "date_source", "validity_rule_used",
        "validity_months", "iibf_registration_number", "holder_name", "csp_code", "ocr_method",
        "extraction_method", "model_provider", "page_count", "pages_readable",
        "readability_reason", "rejection_reason")}
    fields["registration_number"] = extraction.get("iibf_registration_number")
    if decision == "READABLE":
        # In plain words: which date and rule were used, the text, what was ignored (app/why.py).
        from .why import explain
        try:
            fields["why"] = explain(extraction, _d(extraction.get("typed_issue_date")))
        except Exception:
            logger.exception("why_failed")
    owner_review = extraction.get("owner_review") if decision == "READABLE" else None
    if owner_review:
        fields["owner_check"] = owner_review
        fields["owner_seen_name"] = extraction.get("owner_seen_name")
    # Upload page: readable, but type or date not confirmed by the rules.
    type_review = extraction.get("type_review") if decision == "READABLE" else None
    if type_review:
        fields["needs_confirmation"] = type_review
    if decision != "READABLE":
        status = DocumentStatus.UNREADABLE
    elif owner_review or type_review:
        status = DocumentStatus.NEEDS_REVIEW
    else:
        status = _status_for(extraction)
    held = checked = None
    if decision == "READABLE":
        # Safety gate: the evidence pack and its risk score (app/safety_gate.py).
        from .safety_gate import assess, queue_reason
        try:
            risk = assess(db, csp.id, doc_type, extraction, issue, expiry, arrived.date())
        except Exception:
            logger.exception("risk_assessment_failed")
            risk = None
        if risk is not None:
            fields["evidence"] = risk.pop("evidence")
            fields["risk"] = risk
            if status in (DocumentStatus.VALID, DocumentStatus.EXPIRED) and risk["level"] == "hold":
                held = queue_reason(risk)
                status, fields["safety_check"] = DocumentStatus.NEEDS_REVIEW, held
            elif status in (DocumentStatus.VALID, DocumentStatus.EXPIRED) and risk["level"] == "check":
                checked = queue_reason(risk)
    reading = dict(
        document_type=doc_type, status=status, extracted_fields=fields, overall_confidence=extraction.get("confidence"),
        extraction_method=extraction.get("extraction_method"),
        issue_date=issue, expiry_date=expiry, validity_rule_used=extraction.get("validity_rule_used"),
        has_explicit_3year_clause=bool(extraction.get("has_explicit_3year_clause")),
        iibf_reg_number=extraction.get("iibf_registration_number"),
        field_confidences=extraction.get("field_confidences"),
        holder_name=extraction.get("holder_name"), validity_months=extraction.get("validity_months"),
        date_source=extraction.get("date_source"), readability=decision,
    )
    if upgrade is None:
        doc = Document(
            csp_id=csp.id, sha256=sha, file_size_bytes=len(data), mime_type=mime_type, storage_path=path,
            uploaded_at=now, is_current=False, upload_channel=channel, original_filename=(filename or "")[:250],
            source_message_id=source_message_id, source_date=source_date, sender_on_sheet=sender_on_sheet, **reading)
        db.add(doc)
    else:
        doc = upgrade        # keeps its id, file, arrival time and channel
        fields["read_again"] = (f"Unreadable when it arrived; read again with the current rules on {now:%d-%m-%Y} "
                                f"when the same file came again ({channel}).")
        for k, v in reading.items():
            setattr(doc, k, v)
    db.flush()
    if owner_review or type_review:
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING,
                                 reason=" ".join(r for r in (owner_review, type_review) if r)[:500]))
    elif held:
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=held[:500]))
    elif status == DocumentStatus.NEEDS_APPROVAL:
        # Dates read by the AI model: "being checked" until a person looks, so
        # it must be in the Review queue (it wasn't before 2026-10-24).
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
            f"Dates read by the AI model ({extraction.get('start_date')} -> {extraction.get('expiry_date')}): "
            "check them on the document and accept, or correct / reject.")[:500]))
    elif extraction.get("validity_check") and status in (DocumentStatus.VALID, DocumentStatus.EXPIRED):
        # Validity by the 1-year rule (no term read): it counts as it is
        # (valid, or expired and to be renewed); a person reads the term and
        # sets it with "Validity stated" in the queue.
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING,
                                 reason=extraction["validity_check"][:500]))
    elif checked:
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=checked[:500]))
    else:
        from .accuracy import maybe_spot_check
        maybe_spot_check(db, doc)
    if decision == "READABLE":
        recompute_current(db, csp, doc_type)
        sync_agreement_row(db, csp)
    vault.place_csp(db, csp)
    return StoreOutcome(decision, doc, extraction.get("readability_reason"))


def recompute_current(db: Session, csp: CSP, doc_type: str) -> Optional[Document]:
    """The one rule for which copy of a document type is current: the newest
    usable, readable copy by issue date, then upload time. Every other copy
    of that type gets is_current=False. Call after anything that adds a
    document, changes its dates, or rejects it."""
    t = canonical_type(doc_type)
    same = [d for d in db.query(Document).filter(Document.csp_id == csp.id)
            if canonical_type(d.document_type) == t]
    # A copy that may belong to another CSP, or held by the safety gate, counts
    # only once a reviewer accepts it.
    usable = [d for d in same if d.status in USABLE and d.readability != "UNREADABLE"
              and not (d.status == DocumentStatus.NEEDS_REVIEW
                       and ((d.extracted_fields or {}).get("owner_check")
                            or (d.extracted_fields or {}).get("needs_confirmation")
                            or (d.extracted_fields or {}).get("safety_check")))]
    today = date.today()

    def rank(d: Document) -> int:
        # PVR section: a valid PVR / character certificate first, then a
        # lifetime Police Clearance Certificate, then an expired PVR.
        if d.validity_rule_used == "PVR_PCC_LIFETIME":
            return 1
        return 2 if d.expiry_date is None or d.expiry_date >= today else 0

    best = max(usable, key=lambda d: (rank(d) if t == "POLICE_VERIFICATION" else 0,
                                      d.issue_date or date.min, d.uploaded_at or datetime.min, d.id or 0),
               default=None)
    for d in same:
        d.is_current = d is best
    return best
