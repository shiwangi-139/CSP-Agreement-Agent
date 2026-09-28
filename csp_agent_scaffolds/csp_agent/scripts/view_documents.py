"""
scripts/view_documents.py
Inspects all scanned documents, their physical storage, detected types,
issue dates, and calculated expirations (3 years for Agreement, 1 year for PV).
"""

from app.db import SessionLocal
from app.models import CSP, Agreement, Document, InboundMessage

def main():
    db = SessionLocal()
    try:
        print("=" * 80)
        print("SCANNED INBOUND MESSAGES & DOCUMENTS AUDIT")
        print("=" * 80)

        inbound_msgs = db.query(InboundMessage).order_by(InboundMessage.received_at.desc()).all()
        print(f"Total Inbound Emails Logged: {len(inbound_msgs)}")
        for msg in inbound_msgs[:5]:
            print(f" - [{msg.received_at}] From: {msg.sender[:35]} | Subject: {msg.subject[:35]} | Status: {msg.status}")

        print("\n" + "=" * 80)
        print("DOCUMENT VAULT & EXTRACTED METADATA")
        print("=" * 80)
        docs = db.query(Document).order_by(Document.id.desc()).all()
        print(f"Total Documents in Vault: {len(docs)}")

        csps = {c.id: c for c in db.query(CSP).all()}
        agrs = {a.id: a for a in db.query(Agreement).all()}

        for doc in docs:
            c = csps.get(doc.csp_id)
            csp_info = f"{c.lookup_code} ({c.name})" if c else f"ID:{doc.csp_id}"
            fields = doc.extracted_fields or {}
            print(f"\nDocument ID #{doc.id}:")
            print(f"  Type          : {doc.document_type}")
            print(f"  Matched CSP   : {csp_info}")
            print(f"  SHA-256 Hash  : {doc.sha256[:16]}...")
            print(f"  Storage Path  : {doc.storage_path}")
            print(f"  Start / Issue : {fields.get('start_date') or 'N/A'}")
            print(f"  Computed Exp  : {fields.get('expiry_date') or 'N/A'}")
            print(f"  Confidence    : {doc.overall_confidence}")
            print(f"  Status        : {doc.status.value}")

        print("\n" + "=" * 80)
        print("ACTIVE AGREEMENT COMPLIANCE RECORDS")
        print("=" * 80)
        agreements = db.query(Agreement).filter(Agreement.is_active.is_(True)).all()
        print(f"Total Active Agreement Records: {len(agreements)}")
        for a in agreements:
            c = csps.get(a.csp_id)
            c_name = c.name if c else 'Unknown'
            c_code = a.current_csp_code or (c.lookup_code if c else 'N/A')
            print(f" - CSP {c_code} ({c_name}):")
            print(f"     Agreement Start : {a.start_date or 'N/A'}")
            print(f"     Agreement Expiry: {a.expiry_date or 'N/A'} (Validity: 3 Years)")
            print(f"     PV/Char Expiry  : {a.police_verification_expiry or 'N/A'} (Validity: 1 Year)")
            print(f"     Renewal Status  : {a.renewal_status.value if a.renewal_status else 'N/A'}")

    finally:
        db.close()

if __name__ == "__main__":
    main()

