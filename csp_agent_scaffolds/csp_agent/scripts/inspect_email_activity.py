"""
scripts/inspect_email_activity.py
Provides full transparency into what emails the agent is opening, reading,
and matching from Gmail and Neon PostgreSQL.
"""

from app.comms.gmail_oauth import get_gmail_service
from app.db import SessionLocal
from app.models import InboundMessage, Document, CSP, Agreement

def main():
    print("=" * 80)
    print("AGENT EMAIL TRACKING & AUDIT INSPECTOR")
    print("=" * 80)

    # 1. Check Authenticated Gmail Account
    try:
        service = get_gmail_service()
        profile = service.users().getProfile(userId="me").execute()
        email_addr = profile.get("emailAddress", "Unknown")
        total_msgs = profile.get("messagesTotal", 0)
        print(f"\n[1] AUTHENTICATED GMAIL ACCOUNT:")
        print(f"    - Mailbox Address : {email_addr}")
        print(f"    - Total Messages  : {total_msgs}")
    except Exception as e:
        print(f"\n[1] GMAIL CONNECTION ERROR: {e}")
        service = None

    # 2. Inbound Emails Processed and Stored in Database
    db = SessionLocal()
    try:
        print(f"\n[2] EMAILS OPENED & LOGGED IN DATABASE (inbound_messages):")
        inbounds = db.query(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(15).all()
        if not inbounds:
            print("    (No inbound emails logged yet in database)")
        else:
            csps = {c.id: c for c in db.query(CSP).all()}
            for idx, msg in enumerate(inbounds, 1):
                print(f"\n  #{idx} ID: {msg.id} | Status: {msg.status} | Received: {msg.received_at}")
                print(f"     From    : {msg.sender}")
                print(f"     Subject : {msg.subject}")
                print(f"     Msg-ID  : {msg.external_message_id}")

                # Check documents extracted from this email's timestamp/csp
                docs = db.query(Document).filter(Document.uploaded_at >= msg.received_at).all() if msg.received_at else []
                csp_match = None
                if docs:
                    d = docs[0]
                    c = csps.get(d.csp_id)
                    if c:
                        csp_match = f"{c.lookup_code} - {c.name}"

                if csp_match:
                    print(f"     Matched : {csp_match}")

        # 3. Live Inbox Scan Peek
        if service:
            print(f"\n[3] LIVE GMAIL SCAN PEEK (What the agent sees right now in your inbox):")
            query = "label:INBOX (CSP OR Agreement OR 'terminal extension' OR 'Police Verification' OR 'IIBF' OR filename:pdf)"
            print(f"    - Filter Query: {query}\n")
            res = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
            msgs = res.get("messages", [])
            print(f"    Found {len(msgs)} candidate emails waiting in Inbox:")

            for i, m in enumerate(msgs, 1):
                msg_data = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                                          metadataHeaders=["Subject", "From", "Date"]).execute()
                headers = {h["name"].lower(): h["value"] for h in msg_data.get("payload", {}).get("headers", [])}
                subj = headers.get("subject", "(No Subject)")
                sender = headers.get("from", "(Unknown Sender)")
                date_hdr = headers.get("date", "")
                snippet = msg_data.get("snippet", "")[:80]
                print(f"    {i}. FROM   : {sender}")
                print(f"       SUBJECT: {subj}")
                print(f"       DATE   : {date_hdr}")
                print(f"       SNIPPET: {snippet}...")
                print("-" * 75)

    finally:
        db.close()

    print("\n" + "=" * 80)
    print("AUDIT COMPLETE")
    print("=" * 80)

if __name__ == "__main__":
    main()

