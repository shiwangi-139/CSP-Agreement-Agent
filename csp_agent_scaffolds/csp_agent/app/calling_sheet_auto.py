"""
app/calling_sheet_auto.py
Autonomous engine that detects unlisted CSPs from inbound emails, extracts
calling sheet attributes, auto-generates Calling Sheet records, and allows promotion.
"""

import logging
import re
import json
from datetime import datetime, timezone
from email.utils import parseaddr
from sqlalchemy.orm import Session

from .models import CSP, InboundMessage, AutoCallingSheetEntry, AgreementEvent, Document
from .email_ingest import classify_email, CSP_CODE_PATTERN, normalize_csp_code

logger = logging.getLogger(__name__)

INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Chandigarh", "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry"
]

INTERNAL_AND_NON_CSP_DOMAINS = {
    "eko.co.in",
    "sbi.co.in",
    "icicibank.com",
    "rbi.org.in"
}

IGNORED_SENDER_PATTERNS = [
    "noreply", "no-reply", "donotreply", "notification", "alerts",
    "shiwangis.journey@gmail.com"
]


def is_internal_or_non_csp(sender: str) -> bool:
    """Returns True if the sender is an internal employee, bank official, or automated alert."""
    if not sender:
        return True
    real_name, email_addr = parseaddr(sender)
    clean = (email_addr or "").strip().lower()
    if not clean:
        return True
    for pat in IGNORED_SENDER_PATTERNS:
        if pat in clean:
            return True
    domain = clean.split("@")[-1] if "@" in clean else ""
    if domain in INTERNAL_AND_NON_CSP_DOMAINS or domain.endswith(".eko.co.in"):
        return True
    return False


def extract_calling_sheet_fields(
    subject: str = "",
    body: str = "",
    sender: str = "",
    external_message_id: str = None
) -> dict:
    """
    Extracts standard Calling Sheet attributes from email headers and content:
    - CSP ID / KO Code
    - CSP Name
    - CSP Mail ID
    - Mobile Phone
    - State
    - Branch Name
    - Circle (LHO)
    - Request Type
    """
    subject = subject or ""
    body = body or ""
    sender = sender or ""
    combined_text = f"{subject} {body}"

    # 1. Extract Candidate CSP / KO Code
    classification = classify_email(subject, body, sender, external_message_id)
    candidate_ko = classification.get("extracted_ko")

    if not candidate_ko:
        codes = [normalize_csp_code(c) for c in CSP_CODE_PATTERN.findall(combined_text)]
        if codes:
            candidate_ko = codes[0]

    # 2. Extract Sender Email and Name
    real_name, email_addr = parseaddr(sender)
    clean_email = email_addr.strip().lower() if email_addr else None

    is_sender_internal = is_internal_or_non_csp(sender)
    if is_sender_internal:
        csp_name = f"CSP {candidate_ko}" if candidate_ko else "Unregistered CSP"
        clean_email = None  # Do not attribute internal corporate email to the CSP
    else:
        # Determine CSP Name from external sender
        csp_name = real_name.strip() if real_name else None
        if not csp_name and clean_email:
            local_part = clean_email.split("@")[0]
            words = re.split(r'[\._\-]', local_part)
            csp_name = " ".join(w.capitalize() for w in words if w and not w.isdigit())

        # Fallback to candidate KO or generic name if name is empty or robotic
        if not csp_name or csp_name.lower() in ("unknown", "info", "admin", "no-reply", "noreply"):
            if candidate_ko:
                csp_name = f"CSP {candidate_ko}"
            else:
                csp_name = "Unregistered CSP"

    # 3. Extract 10-Digit Mobile Phone
    phone = None
    phone_matches = re.findall(r'\b[6-9]\d{9}\b', combined_text)
    if phone_matches:
        phone = phone_matches[0]

    # 4. Extract State
    detected_state = None
    for state in INDIAN_STATES:
        if re.search(r'\b' + re.escape(state) + r'\b', combined_text, re.IGNORECASE):
            detected_state = state
            break

    # Check for known city/kiosk keywords if state not explicitly found
    if not detected_state:
        if re.search(r'\bchandigarh\b', combined_text + " " + sender, re.IGNORECASE):
            detected_state = "Chandigarh"
        elif re.search(r'\bpatna\b|\bvaishali\b|\bmuzaffarpur\b', combined_text + " " + sender, re.IGNORECASE):
            detected_state = "Bihar"
        elif re.search(r'\blucknow\b|\bkanpur\b|\bvaranasi\b', combined_text + " " + sender, re.IGNORECASE):
            detected_state = "Uttar Pradesh"

    # 5. Extract Branch / Kiosk Name
    detected_branch = None
    if "chandigarh kiosk" in sender.lower() or "chandigarh" in subject.lower():
        detected_branch = "Chandigarh Kiosk"
    elif "patna kiosk" in sender.lower() or "patna" in subject.lower():
        detected_branch = "Patna Kiosk"
    else:
        branch_match = re.search(r'([A-Za-z]+(?:\s+[A-Za-z]+)?\s+(?:Branch|Kiosk|CSP))', combined_text, re.IGNORECASE)
        if branch_match:
            detected_branch = branch_match.group(1).title()

    # 6. Extract Circle (LHO)
    detected_circle = None
    lho_match = re.search(r'LHO\s*([A-Za-z]+)', combined_text + " " + sender, re.IGNORECASE)
    if lho_match:
        detected_circle = f"LHO {lho_match.group(1).upper()}"
    elif detected_state:
        detected_circle = f"Circle {detected_state}"

    # 7. Request Category
    req_type = classification["category"].value if hasattr(classification["category"], "value") else str(classification["category"])

    return {
        "csp_code": candidate_ko,
        "csp_name": csp_name,
        "csp_email": clean_email,
        "phone": phone,
        "state": detected_state or "Pending Verification",
        "branch": detected_branch or "Main Kiosk",
        "circle": detected_circle or "General Circle",
        "terminal_status": "AUTO_DETECTED_FROM_EMAIL",
        "request_type": req_type,
        "source_message_id": external_message_id,
        "source_subject": subject[:250] if subject else "No Subject",
        "confidence": classification.get("confidence", 0.8)
    }


def detect_and_auto_make_calling_sheet(db: Session, max_messages: int = 500) -> dict:
    """
    Scans inbound messages, identifies unlisted CSP activity not in 'Calling Sheet New',
    and auto-creates structured Calling Sheet records in auto_calling_sheet_entries.
    """
    now = datetime.now(timezone.utc)
    summary = {
        "total_scanned": 0,
        "detected_missing": 0,
        "inserted": 0,
        "updated": 0,
        "already_exists": 0
    }

    # Pre-load master CSP lookup
    all_csps = db.query(CSP).all()
    master_codes = set()
    master_emails = set()

    for c in all_csps:
        if c.current_code:
            master_codes.add(c.current_code.upper())
        if c.lookup_code:
            master_codes.add(c.lookup_code.upper())
        if c.email:
            master_emails.add(c.email.strip().lower())

    # Pre-load existing auto-generated calling sheet entries
    existing_auto_entries = db.query(AutoCallingSheetEntry).all()
    auto_code_map = {e.csp_code.upper(): e for e in existing_auto_entries if e.csp_code}
    auto_email_map = {e.csp_email.lower(): e for e in existing_auto_entries if e.csp_email}

    # Scan Inbound Messages
    inbound_msgs = db.query(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(max_messages).all()
    summary["total_scanned"] = len(inbound_msgs)

    for msg in inbound_msgs:
        subject = msg.subject or ""
        sender = msg.sender or ""
        audit = msg.classification_audit or {}

        # Extract Calling Sheet attributes
        fields = extract_calling_sheet_fields(
            subject=subject,
            body="",
            sender=sender,
            external_message_id=msg.external_message_id
        )

        extracted_ko = fields.get("csp_code")
        extracted_email = fields.get("csp_email")
        req_type = fields.get("request_type")

        # Exclude internal Eko staff, interns, and bank domains unless an explicit CSP code is being processed
        if is_internal_or_non_csp(sender) and not extracted_ko:
            continue

        # Determine if this message is CSP activity
        is_csp_activity = req_type not in ("UNKNOWN", "None") or any(
            kw in subject.lower() for kw in ["terminal", "reset", "extension", "agreement", "csp", "pvr", "ko", "police", "character", "iibf"]
        )

        if not is_csp_activity and not extracted_ko:
            continue

        # Check if already present in Calling Sheet New master records
        is_in_master = False
        if extracted_ko and extracted_ko.upper() in master_codes:
            is_in_master = True
        if extracted_email and extracted_email in master_emails:
            is_in_master = True

        if is_in_master:
            # Already part of Calling Sheet New!
            continue

        summary["detected_missing"] += 1

        # Check if we already have this in auto_calling_sheet_entries
        existing_entry = None
        if extracted_ko and extracted_ko.upper() in auto_code_map:
            existing_entry = auto_code_map[extracted_ko.upper()]
        elif extracted_email and extracted_email in auto_email_map:
            existing_entry = auto_email_map[extracted_email]

        if existing_entry:
            # Update fields if current has missing data
            updated = False
            if not existing_entry.phone and fields["phone"]:
                existing_entry.phone = fields["phone"]
                updated = True
            if not existing_entry.csp_code and extracted_ko:
                existing_entry.csp_code = extracted_ko
                updated = True
            if (not existing_entry.state or existing_entry.state == "Pending Verification") and fields["state"] != "Pending Verification":
                existing_entry.state = fields["state"]
                updated = True
            if updated:
                summary["updated"] += 1
            else:
                summary["already_exists"] += 1
        else:
            # Auto-create Calling Sheet Entry!
            new_entry = AutoCallingSheetEntry(
                csp_code=extracted_ko or f"AUTO_{msg.id}",
                csp_name=fields["csp_name"],
                csp_email=extracted_email,
                phone=fields["phone"],
                state=fields["state"],
                branch=fields["branch"],
                circle=fields["circle"],
                terminal_status="AUTO_DETECTED_FROM_EMAIL",
                request_type=fields["request_type"],
                source_message_id=msg.external_message_id,
                source_subject=fields["source_subject"],
                detected_at=now,
                is_promoted_to_master=False,
                extracted_metadata={"confidence": fields["confidence"], "source_inbound_id": msg.id}
            )
            db.add(new_entry)
            db.flush()
            if extracted_ko:
                auto_code_map[extracted_ko.upper()] = new_entry
            if extracted_email:
                auto_email_map[extracted_email] = new_entry
            summary["inserted"] += 1

    db.commit()
    logger.info(f"Auto-generated calling sheet summary: {summary}")
    return summary


def promote_auto_entry_to_csp(db: Session, entry_id: int) -> CSP:
    """
    Promotes an auto-detected calling sheet entry into the master CSP database,
    enabling continuous agreement and compliance tracking immediately.
    """
    entry = db.query(AutoCallingSheetEntry).filter(AutoCallingSheetEntry.id == entry_id).first()
    if not entry:
        raise ValueError(f"AutoCallingSheetEntry with ID {entry_id} not found.")

    if entry.is_promoted_to_master and entry.promoted_csp_id:
        existing = db.query(CSP).filter(CSP.id == entry.promoted_csp_id).first()
        if existing:
            return existing

    # Check if CSP with current code already exists
    existing_csp = None
    if entry.csp_code:
        existing_csp = db.query(CSP).filter(
            (CSP.current_code == entry.csp_code) | (CSP.lookup_code == entry.csp_code)
        ).first()

    now = datetime.now(timezone.utc)

    if existing_csp:
        csp = existing_csp
        if entry.csp_email and not csp.email:
            csp.email = entry.csp_email
        if entry.phone and not csp.phone:
            csp.phone = entry.phone
    else:
        csp = CSP(
            name=entry.csp_name,
            current_code=entry.csp_code,
            lookup_code=entry.csp_code,
            email=entry.csp_email,
            phone=entry.phone,
            region=entry.circle,
            branch=entry.branch,
            status="AUTO_GENERATED",
            preferred_language="ENGLISH",
            calling_sheet_synced_at=None  # Flagged as auto-generated (not from original sheet)
        )
        db.add(csp)
        db.flush()

    # Link back to AutoCallingSheetEntry
    entry.is_promoted_to_master = True
    entry.promoted_csp_id = csp.id
    entry.promoted_at = now

    # Record audit event
    evt = AgreementEvent(
        csp_id=csp.id,
        event_type="CSP_AUTO_PROMOTED_FROM_CALLING_SHEET",
        source="SYSTEM",
        notes=f"Promoted from auto-detected entry ID {entry.id} (Subject: {entry.source_subject})",
        payload={"entry_id": entry.id, "subject": entry.source_subject},
        sent_at=now
    )
    db.add(evt)
    db.commit()

    db.refresh(csp)
    return csp


def promote_all_auto_entries_to_csp(db: Session) -> dict:
    """
    Bulk-promotes all unpromoted auto-generated calling sheet entries.
    """
    unpromoted = db.query(AutoCallingSheetEntry).filter(
        AutoCallingSheetEntry.is_promoted_to_master.is_(False)
    ).all()

    promoted_count = 0
    errors = 0

    for entry in unpromoted:
        try:
            promote_auto_entry_to_csp(db, entry.id)
            promoted_count += 1
        except Exception as e:
            logger.error(f"Error promoting entry {entry.id}: {e}")
            errors += 1

    return {
        "total_unpromoted": len(unpromoted),
        "promoted_count": promoted_count,
        "errors": errors
    }

