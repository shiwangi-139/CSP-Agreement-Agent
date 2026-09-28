"""
scripts/inspect_full_mailbox_candidates.py
Developer inspection tool to preview all candidate emails across Inbox and Trash,
detect CSP codes and phone numbers, and match against the master 536 CSP calling sheet.
"""

import re
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.comms.gmail_oauth import fetch_incoming_csp_emails, get_gmail_service
from app.db import SessionLocal
from app.models import CSP, Document, InboundMessage
from app.config import CSP_CODE_REGEX
from app.validation import normalize_csp_code

CSP_CODE_PATTERN = re.compile(CSP_CODE_REGEX)


def inspect_mailbox(limit: int = 150):
    print("=" * 80)
    print("DEVELOPER FULL MAILBOX & TRASH INSPECTOR")
    print("=" * 80)

    db = SessionLocal()
    try:
        master_csps = {c.lookup_code: c for c in db.query(CSP).all()}
        master_by_phone = {re.sub(r'\D', '', c.phone)[-10:]: c for c in db.query(CSP).all() if c.phone and len(re.sub(r'\D', '', c.phone)) >= 10}
        master_by_email = {c.email.strip().lower(): c for c in db.query(CSP).all() if c.email}
        print(f"Loaded {len(master_csps)} master CSPs from Neon PostgreSQL.")

        print(f"\nScanning mailbox (Inbox, Trash, Sent) for agreements and PDFs up to {limit} messages...")
        emails = fetch_incoming_csp_emails(max_results=50, max_total=limit, include_trash=True, download_attachments=False)
        print(f"Total candidate messages retrieved: {len(emails)}\n")

        matched_count = 0
        unmatched_count = 0
        total_pdfs = 0
        matched_csps_set = set()

        print(f"{'#':<4} | {'FOLDER':<7} | {'CSP CODE':<10} | {'MATCH STATUS':<22} | {'PDFs':<4} | {'SUBJECT':<35}")
        print("-" * 95)

        for i, em in enumerate(emails, 1):
            folder = em.get("folder", "INBOX")
            subject = em.get("subject", "(No Subject)")
            sender = em.get("sender", "")
            attachments = em.get("attachments", [])
            pdf_atts = [a for a in attachments if a["filename"].lower().endswith(".pdf")]
            total_pdfs += len(pdf_atts)

            # Look for CSP codes in subject, body, and filenames
            search_str = f"{subject} " + " ".join(a["filename"] for a in attachments)
            codes = [normalize_csp_code(c) for c in CSP_CODE_PATTERN.findall(search_str)]

            # Check phone numbers
            phones = re.findall(r'\b[6-9]\d{9}\b', search_str + " " + em.get("body", "")[:300])

            # Check email
            sender_em = re.search(r'[\w\.-]+@[\w\.-]+', sender)
            sender_email_clean = sender_em.group(0).lower() if sender_em else ""

            matched_csp = None
            match_method = "None"

            # 1. Match by code
            for c in codes:
                if c in master_csps:
                    matched_csp = master_csps[c]
                    match_method = f"Code ({c})"
                    break

            # 2. Match by phone
            if not matched_csp:
                for ph in phones:
                    if ph in master_by_phone:
                        matched_csp = master_by_phone[ph]
                        match_method = f"Phone ({ph})"
                        break

            # 3. Match by email
            if not matched_csp and sender_email_clean in master_by_email:
                matched_csp = master_by_email[sender_email_clean]
                match_method = f"Email ({sender_email_clean[:12]}..)"

            csp_display = matched_csp.lookup_code if matched_csp else (codes[0] if codes else "--")
            if matched_csp:
                matched_count += 1
                matched_csps_set.add(matched_csp.lookup_code)
                status_str = f"MATCH: {matched_csp.name[:14]}"
            else:
                unmatched_count += 1
                status_str = "UNMATCHED (Check PDF)" if pdf_atts else "NO PDF"

            print(f"{i:<4} | {folder:<7} | {csp_display:<10} | {status_str:<22} | {len(pdf_atts):<4} | {subject[:35]}")

        print("=" * 95)
        print(f"SUMMARY AUDIT:")
        print(f"  - Total Messages Inspected : {len(emails)}")
        print(f"  - Total Attached PDFs Found: {total_pdfs}")
        print(f"  - Unique Master CSPs Matched: {len(matched_csps_set)}")
        print(f"  - Matches by Metadata      : {matched_count}")
        print(f"  - Unmatched (Require PDF text inspection): {unmatched_count}")
        print("=" * 95)

    finally:
        db.close()


if __name__ == "__main__":
    limit_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    inspect_mailbox(limit=limit_arg)
