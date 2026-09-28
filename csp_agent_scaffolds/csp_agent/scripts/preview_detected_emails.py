"""
scripts/preview_detected_emails.py
Dry-run diagnostic tool to show EXACTLY how the agent scans your Gmail inbox.
- Connects using Google Cloud OAuth (gmail_token.json).
- Shows which emails pass the server-side query filter.
- Shows extracted CSP codes from subject, body, and attachments.
- Shows detected PDF attachments without altering database records or marking emails as read.
"""

import re
import logging
from app.comms.gmail_oauth import fetch_incoming_csp_emails
from app.config import CSP_CODE_REGEX
from app.db import SessionLocal
from app.models import CSP
from app.validation import normalize_csp_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CSP_CODE_PATTERN = re.compile(CSP_CODE_REGEX)


def preview_gmail_detection():
    print("=" * 70)
    print("GMAIL OAUTH DETECTION INSPECTOR (DRY RUN)")
    print("Server-side Filter: label:INBOX (CSP OR Agreement OR 'terminal extension' OR 'Police Verification' OR 'IIBF' OR filename:pdf)")
    print("=" * 70)

    emails = fetch_incoming_csp_emails(max_results=15)
    print(f"\nTotal candidate emails retrieved from Gmail server: {len(emails)}\n")

    if not emails:
        print("No matching candidate emails found in INBOX.")
        print("Tip: Send a test email to your account with subject 'CSP Agreement 1A852474' and attach a PDF.")
        return

    db = SessionLocal()
    try:
        for idx, email_item in enumerate(emails, start=1):
            subject = email_item["subject"]
            sender = email_item["sender"]
            date_str = email_item["date"]
            body = email_item["body"]
            attachments = email_item["attachments"]

            print(f"\n--- [Email #{idx}] ---")
            print(f"Subject    : {subject}")
            print(f"From       : {sender}")
            print(f"Date       : {date_str}")
            print(f"Attachments: {len(attachments)} file(s)")
            for att in attachments:
                size_kb = round(len(att["data"]) / 1024, 1)
                print(f"   -> File: {att['filename']} ({att['mime_type']}, {size_kb} KB)")

            # Step 2: Extract CSP codes
            search_text = f"{subject} {body} " + " ".join(att["filename"] for att in attachments)
            raw_codes = CSP_CODE_PATTERN.findall(search_text)
            normalized_codes = list(set([normalize_csp_code(c) for c in raw_codes]))

            print(f"Extracted Codes : {normalized_codes if normalized_codes else 'None found in text'}")

            # Step 3: Match against CSP Database
            matched_csp = None
            if normalized_codes:
                matched_csp = db.query(CSP).filter(
                    (CSP.lookup_code == normalized_codes[0]) | (CSP.current_code == normalized_codes[0])
                ).first()

            if not matched_csp and sender:
                sender_email_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
                if sender_email_match:
                    sender_email = sender_email_match.group(0).lower()
                    matched_csp = db.query(CSP).filter(CSP.email.ilike(sender_email)).first()
                    if matched_csp:
                        print(f"Matched by email: {sender_email}")

            if matched_csp:
                print(f"Decision   : MATCHED CSP in Database! -> [ID: {matched_csp.id}] Name: {matched_csp.name}, Branch: {matched_csp.branch}")
            else:
                if attachments:
                    print("Decision   : Unmatched CSP code in text, will inspect PDF content directly for CSP codes & dates.")
                else:
                    print("Decision   : No CSP code and no PDF attachments -> IGNORED safely.")

        print("\n" + "=" * 70)
        print("Preview completed. No emails were marked as read and no DB records were modified.")
        print("=" * 70)

    finally:
        db.close()


if __name__ == "__main__":
    preview_gmail_detection()

