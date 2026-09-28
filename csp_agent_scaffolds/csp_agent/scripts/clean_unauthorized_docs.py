"""
scripts/clean_unauthorized_docs.py
Safe, Auditable Legacy Remediation Script:
1. Identifies and safely purges unauthorized/unknown documents (PAN, Aadhaar, forms)
   currently residing in storage and DB.
2. Corrects Character Certificate validity: rolls back erroneous 3-year expiry to the
   mandatory 1-year compliance rule (expiry = issue + 1 year).
3. Backfills structured columns (issue_date, expiry_date, validity_rule_used, is_current, iibf_reg_number)
   for existing legitimate agreements, PVRs, and IIBF certificates.
"""

import os
import sys
import logging
from datetime import date, datetime

from app.db import SessionLocal
from app.models import Document, DocumentStatus, CSP, Agreement
from app.storage import delete_file
from app.vault import abs_path, is_inside_vault
from app.expiry_engine import calculate_document_expiry, add_years

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clean_unauthorized_docs")


def clean_legacy_data():
    db = SessionLocal()
    try:
        # -------------------------------------------------------------
        # STEP 1: PURGE UNAUTHORIZED DOCUMENTS (PAN, AADHAAR, FORMS)
        # -------------------------------------------------------------
        unauth_docs = db.query(Document).filter(
            (Document.document_type.in_(["ANNEXURE_OR_OTHER", "UNKNOWN"])) |
            (Document.document_type.is_(None))
        ).all()

        logger.info(f"Discovered {len(unauth_docs)} unauthorized/unknown documents for purge...")
        purged_files = 0

        for d in unauth_docs:
            # Only ever delete inside this project's document vault.
            if d.storage_path:
                path = abs_path(d.storage_path)
                if path is not None and path.is_file() and is_inside_vault(path) and delete_file(str(path)):
                    purged_files += 1

            d.status = DocumentStatus.REJECTED
            d.storage_path = None
            d.is_current = False
            fields = d.extracted_fields or {}
            fields["compliance_audit"] = "PURGED_UNAUTHORIZED_DOCUMENT_AT_ALLOWLIST_GATE"
            d.extracted_fields = fields

        db.commit()
        logger.info(f"Successfully purged {purged_files} unauthorized physical files and updated DB records.")

        # -------------------------------------------------------------
        # STEP 2: CORRECT CHARACTER CERTIFICATE EXPIRY (1-YEAR MANDATE)
        # -------------------------------------------------------------
        cc_docs = db.query(Document).filter(
            Document.document_type.in_(["CHARACTER_CERTIFICATE", "POLICE_VERIFICATION"])
        ).all()

        cc_corrected = 0
        for d in cc_docs:
            fields = d.extracted_fields or {}
            start_str = fields.get("start_date") or fields.get("issue_date") or fields.get("police_verification_date")
            if isinstance(start_str, dict):
                start_str = start_str.get("value")

            if start_str:
                try:
                    start_d = date.fromisoformat(str(start_str))
                    # Mandatory 1-year rule
                    correct_exp = add_years(start_d, 1)
                    d.issue_date = start_d
                    d.expiry_date = correct_exp
                    d.validity_rule_used = "PVR_DEFAULT_1_YEAR"
                    fields["expiry_date"] = correct_exp.isoformat()
                    d.extracted_fields = fields
                    cc_corrected += 1
                except Exception as ex:
                    logger.warning(f"Could not parse CC date for doc {d.id}: {ex}")

        db.commit()
        logger.info(f"Corrected {cc_corrected} Police Verification / Character Certificate expiry dates to 1-year mandate.")

        # -------------------------------------------------------------
        # STEP 3: BACKFILL AGREEMENT & IIBF STRUCTURED COLUMNS
        # -------------------------------------------------------------
        legit_docs = db.query(Document).filter(
            Document.document_type.in_(["AGREEMENT", "CSP_AGREEMENT", "IIBF_CERTIFICATE", "IIBF_CERTIFICATION"])
        ).all()

        backfilled = 0
        for d in legit_docs:
            fields = d.extracted_fields or {}
            start_str = fields.get("start_date")
            exp_str = fields.get("expiry_date")

            if isinstance(start_str, dict):
                start_str = start_str.get("value")
            if isinstance(exp_str, dict):
                exp_str = exp_str.get("value")

            start_d = date.fromisoformat(str(start_str)) if start_str else None
            exp_d = date.fromisoformat(str(exp_str)) if exp_str and str(exp_str) != "LIFETIME_NO_EXPIRY" else None

            d.issue_date = start_d
            d.expiry_date = exp_d

            if d.document_type in ("AGREEMENT", "CSP_AGREEMENT"):
                has_3y = fields.get("has_explicit_3year_clause", True)  # preserve legacy 3-yr if marked
                d.has_explicit_3year_clause = bool(has_3y)
                d.validity_rule_used = "AGREEMENT_EXPLICIT_3_YEAR" if has_3y else "AGREEMENT_DEFAULT_1_YEAR"
            elif d.document_type in ("IIBF_CERTIFICATE", "IIBF_CERTIFICATION"):
                d.validity_rule_used = "IIBF_LIFETIME_NO_EXPIRY"
                raw_reg = fields.get("registration_number") or fields.get("membership_number")
                if isinstance(raw_reg, dict):
                    raw_reg = raw_reg.get("value")
                d.iibf_reg_number = str(raw_reg) if raw_reg else None

            backfilled += 1

        db.commit()
        logger.info(f"Backfilled structured compliance columns for {backfilled} agreements and IIBF certificates.")

    finally:
        db.close()


if __name__ == "__main__":
    clean_legacy_data()

