"""
End-to-end test runner for the CSP Email Ingestion & AI Extraction Pipeline.
Connects to Gmail, processes read & unread CSP agreement emails, extracts details using
Gemini 3.6 Flash, writes to Neon PostgreSQL, and displays the saved database records.
"""
import json
import logging
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

# Setup readable console logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_email_pipeline")

from app.db import SessionLocal
from app.models import CSP, Document, InboundMessage, ManualReviewQueue, AgreementEvent
from app.email_ingest import run_email_ingestion_sync


def run_pipeline():
    print("\n" + "=" * 80)
    print("🚀 STARTING CSP EMAIL INGESTION & AI EXTRACTION PIPELINE TEST")
    print("=" * 80)

    if "--reset" in sys.argv or "--force" in sys.argv:
        db = SessionLocal()
        try:
            print("\n[Reset] Clearing previous test documents, review queue, and messages for fresh run...")
            db.query(ManualReviewQueue).delete()
            db.query(AgreementEvent).delete()
            db.query(Document).delete()
            db.query(InboundMessage).delete()
            db.commit()
            print("[Reset] Database cleared successfully.")
        finally:
            db.close()

    print("\n[Step 1] Polling Gmail mailbox and running ingestion...")
    summary = run_email_ingestion_sync()
    print(f"\n[Result] Ingestion Run Summary: {summary}")

    print("\n" + "=" * 80)
    print("📊 INSPECTING STORED RECORDS IN NEON POSTGRESQL DATABASE")
    print("=" * 80)

    db = SessionLocal()
    try:
        # 1. Inbound Messages
        inbounds = db.query(InboundMessage).order_by(InboundMessage.id.desc()).all()
        print(f"\n📨 Inbound Messages ({len(inbounds)} found):")
        print("-" * 80)
        for im in inbounds:
            print(f"  ID: {im.id} | Status: {im.status} | From: {im.sender} | Subject: {im.subject}")
            if im.error_message:
                print(f"     Error/Note: {im.error_message}")

        # 2. CSPs
        csps = db.query(CSP).order_by(CSP.id.asc()).all()
        print(f"\n👤 CSP Records ({len(csps)} found):")
        print("-" * 80)
        for c in csps:
            print(f"  ID: {c.id} | Code: {c.lookup_code} | Name: {c.name} | Email: {c.email or 'N/A'} | Phone: {c.phone or 'N/A'}")

        # 3. Documents
        docs = db.query(Document).order_by(Document.id.asc()).all()
        print(f"\n📄 Stored Documents & AI Extractions ({len(docs)} found):")
        print("-" * 80)
        for d in docs:
            print(f"  Doc ID: {d.id} | CSP ID: {d.csp_id} | Type: {d.document_type} | Status: {d.status.value if hasattr(d.status, 'value') else d.status} | Confidence: {d.overall_confidence}")
            print(f"  Storage: {d.storage_path} | Size: {d.file_size_bytes} bytes")
            if d.extracted_fields:
                print(f"  Extracted Fields:")
                print("   ", json.dumps(d.extracted_fields, indent=4).replace("\n", "\n    "))
            print("-" * 40)

        # 4. Review Queue
        reviews = db.query(ManualReviewQueue).order_by(ManualReviewQueue.id.asc()).all()
        print(f"\n📋 Manual Review Queue ({len(reviews)} items):")
        print("-" * 80)
        for r in reviews:
            print(f"  Review ID: {r.id} | Document ID: {r.document_id} | Reason: {r.reason}")

        # 5. Agreement Events
        events = db.query(AgreementEvent).order_by(AgreementEvent.id.desc()).limit(10).all()
        print(f"\n📝 Recent Agreement Events Audit Log (last {len(events)}):")
        print("-" * 80)
        for ev in reversed(events):
            print(f"  Event ID: {ev.id} | Type: {ev.event_type} | Source: {ev.source} | Notes: {ev.notes}")

    finally:
        db.close()

    print("\n" + "=" * 80)
    print("✅ TEST PIPELINE RUN COMPLETE")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_pipeline()
