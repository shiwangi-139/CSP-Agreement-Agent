import enum
from sqlalchemy import (
    Column, Integer, String, Date, Boolean, Float, BigInteger,
    ForeignKey, DateTime, Text, Enum as SQLEnum, func, UniqueConstraint, Index, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from .db import Base


class RenewalStatus(enum.Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PENDING_RENEWAL = "PENDING_RENEWAL"
    RENEWED = "RENEWED"
    EXPIRED_LOCKED = "EXPIRED_LOCKED"


class DocumentStatus(enum.Enum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    EXTRACTED_DETERMINISTIC = "EXTRACTED_DETERMINISTIC"
    EXTRACTED_AI = "EXTRACTED_AI"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    VALID = "VALID"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    MANUAL_VERIFIED = "MANUAL_VERIFIED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    # Blurry / unreadable after the full OCR + model chain. Kept for audit,
    # but counts as MISSING for categories and outreach (never reviewed).
    UNREADABLE = "UNREADABLE"
    EXPIRED = "EXPIRED"


class DocumentType(enum.Enum):
    AGREEMENT = "AGREEMENT"
    POLICE_VERIFICATION = "POLICE_VERIFICATION"
    CHARACTER_CERTIFICATE = "CHARACTER_CERTIFICATE"
    IIBF_CERTIFICATE = "IIBF_CERTIFICATE"
    UNKNOWN = "UNKNOWN"


class EmailCategory(enum.Enum):
    TERMINAL_RESET = "TERMINAL_RESET"
    TERMINAL_EXTENSION = "TERMINAL_EXTENSION"
    AGREEMENT_DOCUMENT = "AGREEMENT_DOCUMENT"
    PVR_DOCUMENT = "PVR_DOCUMENT"
    CHARACTER_CERTIFICATE = "CHARACTER_CERTIFICATE"
    IIBF_DOCUMENT = "IIBF_DOCUMENT"
    GENERAL_CSP_COMMUNICATION = "GENERAL_CSP_COMMUNICATION"
    UNKNOWN = "UNKNOWN"


class OutboundStatus(enum.Enum):
    DRAFT = "DRAFT"
    QUEUED_FOR_REVIEW = "QUEUED_FOR_REVIEW"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    # Approved, but the channel has no live provider yet (WHATSAPP_MODE=stub).
    READY_NOT_SENT = "READY_NOT_SENT"
    # Recipient guard refused the destination (not on the calling sheet).
    BLOCKED = "BLOCKED"


class ReviewStatus(enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"


class CSP(Base):
    __tablename__ = "csp"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String)
    email = Column(String)
    whatsapp_number = Column(String)
    current_code = Column(String, unique=True)
    lookup_code = Column(String, unique=True, index=True)
    rm_id = Column(Integer, ForeignKey("internal_users.id"))
    dc_id = Column(Integer, ForeignKey("internal_users.id"))
    lho_id = Column(Integer, ForeignKey("internal_users.id"))
    status = Column(String, default="ACTIVE")
    region = Column(String)
    branch = Column(String)
    kiosk_location = Column(String)
    preferred_language = Column(String, default="ENGLISH")
    calling_sheet_synced_at = Column(DateTime)
    sheet_row_version = Column(String)
    is_active_in_calling_sheet = Column(Boolean, default=True, index=True)
    has_missing_contact = Column(Boolean, default=False)
    alt_phone = Column(String)
    state = Column(String)
    circle = Column(String)
    branch_code = Column(String)
    terminal_status = Column(String)
    # {"csp": [...], "rm": [...], "dc": [...]} -- which contact details are
    # missing, computed on every calling-sheet load.
    contact_gaps = Column(JSONB)
    # Compliance category, stored (see app/compliance.py): 1 ACTIVE,
    # 2 PARTIAL, 3 EXPIRED, 4 NONE.
    category = Column(Integer, index=True)
    category_reason = Column(String)
    sub_slab = Column(String, index=True)   # e.g. "2.3" = slab 2, IIBF missing (app/compliance.py: SUB_SLABS)
    category_updated_at = Column(DateTime)
    next_action_at = Column(Date, index=True)

    agreements = relationship("Agreement", back_populates="csp")
    documents = relationship("Document", back_populates="csp")


class InternalUser(Base):
    __tablename__ = "internal_users"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True)
    role = Column(String)  # RM, DC, LHO, ADMIN
    phone = Column(String)
    password_hash = Column(String)
    # Dashboard login (app/auth.py). Only ADMIN and RM users with a password can log in.
    login_enabled = Column(Boolean, default=False)
    failed_logins = Column(Integer, default=0)
    locked_until = Column(DateTime)
    last_login_at = Column(DateTime)


class CspExtraPhone(Base):
    """Extra contact numbers for a CSP from a separate contact sheet
    (scripts/import_contact_numbers.py). Kept apart from the calling sheet,
    which seniors maintain and which stays the main source."""
    __tablename__ = "csp_extra_phones"
    __table_args__ = (UniqueConstraint("csp_id", "phone", name="uq_csp_extra_phone"),)
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    phone = Column(String, nullable=False, index=True)        # 10 digits
    source_column = Column(String)                             # Mobile Number / Home Phone / Work Phone
    source_file = Column(String)
    added_at = Column(DateTime, server_default=func.now())


class AdminSession(Base):
    """A logged-in dashboard session. Only the SHA-256 of the cookie value is
    stored, so a database leak does not hand out live sessions."""
    __tablename__ = "admin_sessions"
    token_hash = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("internal_users.id"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)
    last_seen_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    ip = Column(String)


class Agreement(Base):
    __tablename__ = "agreements"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    start_date = Column(Date)
    expiry_date = Column(Date, nullable=True, index=True)
    police_verification_expiry = Column(Date, index=True)
    current_csp_code = Column(String)
    is_active = Column(Boolean, default=True)
    renewal_status = Column(SQLEnum(RenewalStatus), default=RenewalStatus.ACTIVE, nullable=False)
    pdf_link = Column(String)
    pdf_hash = Column(String)

    csp = relationship("CSP", back_populates="agreements")


class CSPCodeHistory(Base):
    __tablename__ = "csp_code_history"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False)
    code = Column(String, nullable=False)
    valid_from = Column(Date)
    valid_to = Column(Date)
    reason = Column(String)


class Document(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    agreement_id = Column(Integer, ForeignKey("agreements.id"), nullable=True)
    document_type = Column(String)  # Will transition to Enum eventually, keeping String for now or mapping it
    sha256 = Column(String, index=True, nullable=False)
    file_size_bytes = Column(BigInteger)
    mime_type = Column(String)
    storage_path = Column(String)
    status = Column(SQLEnum(DocumentStatus), default=DocumentStatus.UPLOADED, nullable=False)
    extracted_fields = Column(JSONB)
    overall_confidence = Column(Float)
    extraction_method = Column(String)
    uploaded_at = Column(DateTime, default=func.now())
    is_current = Column(Boolean, default=True, index=True)
    upload_channel = Column(String, default="GMAIL_INBOUND")
    original_filename = Column(String, nullable=True)
    issue_date = Column(Date, nullable=True, index=True)
    expiry_date = Column(Date, nullable=True, index=True)
    validity_rule_used = Column(String, nullable=True)
    has_explicit_3year_clause = Column(Boolean, default=False)
    iibf_reg_number = Column(String, nullable=True, index=True)
    field_confidences = Column(JSONB, nullable=True)
    holder_name = Column(String, nullable=True)
    validity_months = Column(Integer, nullable=True)
    # Which rule found the issue date, e.g. AGREEMENT_ESTAMP_CERT_ISSUE_DATE.
    date_source = Column(String, nullable=True)
    # READABLE / UNREADABLE / NOT_ALLOWED -- the final decision for this file.
    readability = Column(String, nullable=True, index=True)
    source_message_id = Column(String, nullable=True, index=True)
    source_date = Column(DateTime, nullable=True)
    # False when the email came from an address not on the calling sheet
    # and not @eko.co.in. Shown on the dashboard; does not block processing.
    sender_on_sheet = Column(Boolean, nullable=True)

    csp = relationship("CSP", back_populates="documents")

    __table_args__ = (
        UniqueConstraint("csp_id", "sha256", name="uq_document_csp_hash"),
    )


class ManualReviewQueue(Base):
    __tablename__ = "manual_review_queue"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    reason = Column(String)
    status = Column(SQLEnum(ReviewStatus), default=ReviewStatus.PENDING, nullable=False)
    assigned_to = Column(Integer, ForeignKey("internal_users.id"), nullable=True)
    created_at = Column(DateTime, default=func.now())
    resolved_at = Column(DateTime)
    correction_notes = Column(Text)


class ExtractionCorrection(Base):
    __tablename__ = "extraction_corrections"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=False)
    field_name = Column(String)
    ai_value = Column(String)
    ai_confidence = Column(Float)
    human_value = Column(String)
    corrected_by = Column(Integer, ForeignKey("internal_users.id"))
    corrected_at = Column(DateTime, default=func.now())


class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    agreement_id = Column(Integer, ForeignKey("agreements.id"), nullable=False, index=True)
    stage = Column(String, nullable=False)
    channel = Column(String, nullable=False)
    idempotency_key = Column(String, unique=True, nullable=False, index=True)
    message_content = Column(Text)
    delivery_status = Column(String, default="PENDING")
    sent_at = Column(DateTime, default=func.now())


class Escalation(Base):
    __tablename__ = "escalations"
    id = Column(Integer, primary_key=True)
    agreement_id = Column(Integer, ForeignKey("agreements.id"), nullable=False, index=True)
    level = Column(String, nullable=False)
    escalated_to = Column(Integer, ForeignKey("internal_users.id"), nullable=True)
    escalated_at = Column(DateTime, default=func.now())
    reason = Column(String)
    resolved_at = Column(DateTime)


class OutboundMessage(Base):
    """Every draft and every sent message. Rows are never deleted, so this
    is the permanent record of what was sent to whom and why."""
    __tablename__ = "outbound_messages"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=True, index=True)
    template_name = Column(String, nullable=False)
    channel = Column(String, nullable=False)  # EMAIL, WHATSAPP
    destination = Column(String, nullable=False)
    payload_json = Column(JSONB)
    status = Column(SQLEnum(OutboundStatus), default=OutboundStatus.DRAFT, nullable=False)
    error_log = Column(Text)
    created_at = Column(DateTime, default=func.now())
    sent_at = Column(DateTime)
    idempotency_key = Column(String, unique=True, nullable=True, index=True)
    cycle_id = Column(Integer, ForeignKey("outreach_cycles.id"), nullable=True, index=True)
    document_type = Column(String, nullable=True)
    stage = Column(String, nullable=True)
    recipient_role = Column(String, nullable=True)  # CSP, RM, DC
    attempts = Column(Integer, default=0)
    next_retry_at = Column(DateTime, nullable=True)
    provider_message_id = Column(String, nullable=True)
    delivery_status = Column(String, nullable=True)
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)


class OutreachCycle(Base):
    """One run of follow-ups for one document of one CSP.

    kind=RENEWAL runs the pre-expiry ladder anchored on the expiry date;
    kind=UPLOAD runs the every-3-days upload-link cycle anchored on the day
    it opened. Counters enforce the escalation caps (RM <= 2, DC <= 1).
    The cycle closes as soon as a newer valid document arrives.
    """
    __tablename__ = "outreach_cycles"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    document_type = Column(String, nullable=False)  # AGREEMENT, POLICE_VERIFICATION, IIBF_CERTIFICATE, ALL
    kind = Column(String, nullable=False)  # RENEWAL, UPLOAD
    ladder = Column(String, nullable=False)  # key into policy.LADDERS
    anchor_date = Column(Date, nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    opened_at = Column(DateTime, default=func.now())
    closed_at = Column(DateTime, nullable=True)
    close_reason = Column(String, nullable=True)
    csp_messages = Column(Integer, default=0, nullable=False)
    rm_messages = Column(Integer, default=0, nullable=False)
    dc_messages = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("uq_open_cycle", "csp_id", "document_type", "kind",
              unique=True, postgresql_where=text("closed_at IS NULL")),
    )


class IngestState(Base):
    """Checkpoint for Gmail ingestion so the 2-year backfill runs once and
    later scans only fetch new mail via the Gmail history API."""
    __tablename__ = "ingest_state"
    key = Column(String, primary_key=True)
    value = Column(JSONB, nullable=False, default=dict)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class PortalToken(Base):
    __tablename__ = "portal_tokens"
    jti = Column(String, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    requested_types = Column(JSONB)
    created_at = Column(DateTime, default=func.now())
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoke_reason = Column(String, nullable=True)


class ContactChangeRequest(Base):
    """Contact edits typed on the upload portal. They never overwrite the
    calling-sheet data directly; someone reviews and updates the sheet."""
    __tablename__ = "contact_change_requests"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=False, index=True)
    field = Column(String, nullable=False)
    old_value = Column(String)
    new_value = Column(String)
    status = Column(String, default="PENDING")
    created_at = Column(DateTime, default=func.now())


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    id = Column(Integer, primary_key=True)
    external_message_id = Column(String, unique=True, index=True)
    sender = Column(String)
    subject = Column(String)
    email_category = Column(SQLEnum(EmailCategory), default=EmailCategory.UNKNOWN)
    ai_classification_fallback = Column(Boolean, default=False)
    classification_audit = Column(JSONB, nullable=True)
    received_at = Column(DateTime, default=func.now())
    status = Column(String, default="RECEIVED")
    error_message = Column(Text)
    processed_at = Column(DateTime)
    thread_id = Column(String, nullable=True)
    sender_on_sheet = Column(Boolean, nullable=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=True, index=True)
    # [{filename, sha256, decision: READABLE|UNREADABLE|NOT_ALLOWED|DUPLICATE, document_id, reason}]
    attachment_decisions = Column(JSONB, nullable=True)


class AgreementEvent(Base):
    __tablename__ = "agreement_events"
    id = Column(Integer, primary_key=True)
    csp_id = Column(Integer, ForeignKey("csp.id"), nullable=True)
    agreement_id = Column(Integer, ForeignKey("agreements.id"), nullable=True)
    event_type = Column(String, nullable=False)
    source = Column(String)
    channel = Column(String)
    payload = Column(JSONB, nullable=True)
    format_version = Column(String)
    doc_link = Column(String)
    response_received_at = Column(DateTime)
    notes = Column(Text)
    sent_at = Column(DateTime, default=func.now())


class AutoCallingSheetEntry(Base):
    """
    Persists auto-detected Calling Sheet entries for CSPs whose details
    are not yet registered in 'Calling Sheet New'.
    """
    __tablename__ = "auto_calling_sheet_entries"

    id = Column(Integer, primary_key=True)
    csp_code = Column(String, index=True, nullable=True)
    csp_name = Column(String, nullable=False)
    csp_email = Column(String, index=True, nullable=True)
    phone = Column(String, nullable=True)
    state = Column(String, nullable=True)
    branch = Column(String, nullable=True)
    circle = Column(String, nullable=True)
    terminal_status = Column(String, default="AUTO_DETECTED_FROM_EMAIL")
    request_type = Column(String, nullable=True)
    source_message_id = Column(String, nullable=True)
    source_subject = Column(String, nullable=True)
    detected_at = Column(DateTime, default=func.now())
    is_promoted_to_master = Column(Boolean, default=False)
    promoted_csp_id = Column(Integer, nullable=True)
    promoted_at = Column(DateTime, nullable=True)
    extracted_metadata = Column(JSONB, nullable=True)

