"""
app/email_ingest.py
Secure, Deterministic Email Ingestion using Google Cloud OAuth 2.0.
- Reads inbound candidate messages using Gmail API.
- Extracts CSP code and matches against pre-seeded CSP master database.
- Runs local deterministic extraction (PyMuPDF + Tesseract) on PDF attachments.
- Links agreement and police verification dates to Neon PostgreSQL.
- Computes Category A, B, C, D compliance.
"""

import asyncio
import logging
import json
import re
from datetime import datetime, timezone, date
from sqlalchemy.exc import IntegrityError

from .config import CSP_CODE_REGEX
from .db import SessionLocal
from .models import (
    CSP, Document, DocumentStatus, Agreement, RenewalStatus,
    InboundMessage, OutboundMessage, AgreementEvent, ManualReviewQueue, ReviewStatus,
    EmailCategory, AutoCallingSheetEntry
)

from .comms.gmail_oauth import fetch_incoming_csp_emails, mark_email_as_seen, get_gmail_service, fetch_attachment_bytes
from .ai.extraction.deterministic_extractor import extract_document_fields_deterministic, add_years
from .validation import normalize_csp_code

logger = logging.getLogger(__name__)
CSP_CODE_PATTERN = re.compile(CSP_CODE_REGEX)


def classify_email(subject: str, body: str = "", sender: str = "", email_id: str = "") -> dict:
    """
    Deterministic subject & body classification layer for CSP emails with audit logging.
    Supports all 8 configured categories:
    TERMINAL_RESET, TERMINAL_EXTENSION, AGREEMENT_DOCUMENT, PVR_DOCUMENT,
    CHARACTER_CERTIFICATE, IIBF_DOCUMENT, GENERAL_CSP_COMMUNICATION, UNKNOWN.
    """
    text = f"{subject} {body}".strip()
    norm_text = text.lower()
    
    # 1. Terminal Reset Detection
    # Examples: "Request to terminal reset for KO", "Request for terminal reset - KO", "Terminal reset request for KO"
    tr_match = re.search(r'(?:request\s+(?:to|for)\s+)?terminal\s+reset|reset\s+(?:of\s+)?terminal', norm_text, re.IGNORECASE)
    if tr_match:
        ko_match = re.search(r'\b(?:ko|k\.o\.)\s*[:#-]?\s*([A-Za-z0-9]+)\b', text, re.IGNORECASE)
        extracted_ko = ko_match.group(1).upper() if ko_match else None
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.TERMINAL_RESET.value,
            "matched_rule": "REGEX_TERMINAL_RESET",
            "confidence": 0.98,
            "ai_used": False,
            "extracted_ko": extracted_ko,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {
            "category": EmailCategory.TERMINAL_RESET,
            "rule": "REGEX_TERMINAL_RESET",
            "confidence": 0.98,
            "ai_used": False,
            "extracted_ko": extracted_ko,
            "audit": audit
        }

    # 2. Terminal Extension Detection
    # Examples: "Request to terminal extension for KO", "Terminal extension request - KO"
    te_match = re.search(r'(?:request\s+(?:to|for)\s+)?terminal\s+extension|extension\s+(?:of\s+)?terminal|extend\s+terminal', norm_text, re.IGNORECASE)
    if te_match:
        ko_match = re.search(r'\b(?:ko|k\.o\.)\s*[:#-]?\s*([A-Za-z0-9]+)\b', text, re.IGNORECASE)
        extracted_ko = ko_match.group(1).upper() if ko_match else None
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.TERMINAL_EXTENSION.value,
            "matched_rule": "REGEX_TERMINAL_EXTENSION",
            "confidence": 0.98,
            "ai_used": False,
            "extracted_ko": extracted_ko,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {
            "category": EmailCategory.TERMINAL_EXTENSION,
            "rule": "REGEX_TERMINAL_EXTENSION",
            "confidence": 0.98,
            "ai_used": False,
            "extracted_ko": extracted_ko,
            "audit": audit
        }

    # 3. Police Verification Report (PVR)
    if re.search(r'police\s+verification|pvr\b|police\s+clearance|police\s+report', norm_text, re.IGNORECASE):
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.PVR_DOCUMENT.value,
            "matched_rule": "KEYWORD_PVR",
            "confidence": 0.95,
            "ai_used": False,
            "extracted_ko": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {"category": EmailCategory.PVR_DOCUMENT, "rule": "KEYWORD_PVR", "confidence": 0.95, "ai_used": False, "extracted_ko": None, "audit": audit}

    # 4. Character Certificate
    if re.search(r'character\s+cert(?:ificate)?', norm_text, re.IGNORECASE):
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.CHARACTER_CERTIFICATE.value,
            "matched_rule": "KEYWORD_CHARACTER_CERT",
            "confidence": 0.95,
            "ai_used": False,
            "extracted_ko": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {"category": EmailCategory.CHARACTER_CERTIFICATE, "rule": "KEYWORD_CHARACTER_CERT", "confidence": 0.95, "ai_used": False, "extracted_ko": None, "audit": audit}

    # 5. IIBF Document
    if re.search(r'\biibf\b|\bbcbf\b|indian\s+institute\s+of\s+banking', norm_text, re.IGNORECASE):
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.IIBF_DOCUMENT.value,
            "matched_rule": "KEYWORD_IIBF",
            "confidence": 0.95,
            "ai_used": False,
            "extracted_ko": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {"category": EmailCategory.IIBF_DOCUMENT, "rule": "KEYWORD_IIBF", "confidence": 0.95, "ai_used": False, "extracted_ko": None, "audit": audit}

    # 6. Agreement Document
    if re.search(r'agreement|contract|renewal\s+agreement|stamp\s+paper', norm_text, re.IGNORECASE):
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.AGREEMENT_DOCUMENT.value,
            "matched_rule": "KEYWORD_AGREEMENT",
            "confidence": 0.92,
            "ai_used": False,
            "extracted_ko": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {"category": EmailCategory.AGREEMENT_DOCUMENT, "rule": "KEYWORD_AGREEMENT", "confidence": 0.92, "ai_used": False, "extracted_ko": None, "audit": audit}

    # 7. General CSP Communication
    if re.search(r'query|issue|help|grievance|complaint|request|inquiry', norm_text, re.IGNORECASE):
        audit = {
            "email_id": email_id,
            "sender": sender,
            "subject": subject,
            "detected_type": EmailCategory.GENERAL_CSP_COMMUNICATION.value,
            "matched_rule": "KEYWORD_GENERAL_COMM",
            "confidence": 0.80,
            "ai_used": False,
            "extracted_ko": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "final_status": "CLASSIFIED"
        }
        return {"category": EmailCategory.GENERAL_CSP_COMMUNICATION, "rule": "KEYWORD_GENERAL_COMM", "confidence": 0.80, "ai_used": False, "extracted_ko": None, "audit": audit}

    # Fallback to UNKNOWN with explicit audit trail
    audit = {
        "email_id": email_id,
        "sender": sender,
        "subject": subject,
        "detected_type": EmailCategory.UNKNOWN.value,
        "matched_rule": "NO_RULE_MATCH_FALLBACK",
        "confidence": 0.20,
        "ai_used": False,
        "extracted_ko": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "final_status": "NEEDS_AI_OR_MANUAL_REVIEW"
    }
    return {
        "category": EmailCategory.UNKNOWN,
        "rule": "NO_RULE_MATCH_FALLBACK",
        "confidence": 0.20,
        "ai_used": False,
        "extracted_ko": None,
        "audit": audit
    }


def classify_email_subject(subject: str, body: str) -> tuple[EmailCategory, bool]:
    """Backwards-compatible wrapper returning (EmailCategory, is_ai_fallback)."""
    res = classify_email(subject, body)
    return res["category"], res["ai_used"]


def evaluate_csp_category(csp: CSP, active_agreement: Agreement | None, documents: list[Document]) -> str:
    """
    Deterministically computes compliance category:
    - CATEGORY_A: Agreement + Police Verification valid and not expired.
    - CATEGORY_B: Incomplete / Missing required documents.
    - CATEGORY_C: Expired agreement or police verification / rejected doc.
    - CATEGORY_D: Zero documents received (Non-responsive).
    """
    today = date.today()

    if not documents:
        return "CATEGORY_D"

    current_docs = [d for d in documents if getattr(d, 'is_current', True)]
    if not current_docs:
        current_docs = documents

    # Check for expired or rejected documents (Category C)
    for d in current_docs:
        if d.status == DocumentStatus.REJECTED:
            return "CATEGORY_C"
        if d.expiry_date and d.expiry_date < today:
            return "CATEGORY_C"

    if active_agreement and active_agreement.expiry_date and active_agreement.expiry_date < today:
        return "CATEGORY_C"

    # Check for complete compliance (Category A requires all 3 documents: Agreement + PV + IIBF)
    valid_types = {d.document_type for d in current_docs if d.status in (DocumentStatus.VALID, DocumentStatus.NEEDS_APPROVAL, DocumentStatus.EXTRACTED_DETERMINISTIC)}
    has_agreement = "AGREEMENT" in valid_types or "CSP_AGREEMENT" in valid_types
    has_pv = "POLICE_VERIFICATION" in valid_types or "CHARACTER_CERTIFICATE" in valid_types
    has_iibf = "IIBF_CERTIFICATE" in valid_types or "IIBF_CERTIFICATION" in valid_types

    if has_agreement and has_pv and has_iibf:
        return "CATEGORY_A"

    # Otherwise partial/missing (Category B)
    return "CATEGORY_B"



def run_email_ingestion_sync(max_results: int = 50, max_total: int = 200, days_back: int = 730,
                             after_date: str | None = None) -> dict:
    """Replaced by app/gmail_ingest.run_scan (read-once, checkpointed).
    Kept so older scripts and routes keep working."""
    from .gmail_ingest import run_scan
    return run_scan(max_messages=max_total)


def run_2year_historical_backfill(max_total: int = 1000) -> dict:
    from .gmail_ingest import run_scan
    return run_scan(max_messages=max_total)


run_email_ingestion = run_email_ingestion_sync
