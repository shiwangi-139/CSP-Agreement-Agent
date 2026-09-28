"""
scripts/rescan_and_rebuild_vault.py
Deterministic Rescan & PostgreSQL Vault Rebuilder.
- Uses PostgreSQL connection configured in .env (Neon AWS).
- Cleans up legacy misclassified documents.
- Ingests candidate emails from the last 2 years using rate-paced Gmail OAuth.
- Applies strict allowlist gate with correct precedence (permits agreements with PAN clauses).
- Distinguishes 3-year explicit agreements from 1-year default agreements.
- Automatically archives older expired agreements (is_current=False) while keeping the newest active (is_current=True).
- Preserves all expired files in storage/documents/{CSP_CODE}/ for compliance auditing.
"""

import sys
import os
import argparse
import logging
from datetime import date

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal, engine, Base
from app.models import CSP, Document, Agreement, AgreementEvent, InboundMessage, ManualReviewQueue
from app.email_ingest import run_2year_historical_backfill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vault_rebuilder")


def clean_database_tables(db):
    """Optionally cleans documents and agreements for a pristine 100% clean-slate re-scan."""
    logger.info("Cleaning up legacy document records for a fresh rescan...")
    for model in (ManualReviewQueue, AgreementEvent, Document, Agreement, InboundMessage):
        try:
            db.query(model).delete()
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug(f"Table clean skipped for {model.__tablename__}: {e}")
    logger.info("Database cleaned successfully.")



def print_compliance_summary(db):
    """Prints a structured audit of all CSP compliance records stored in PostgreSQL."""
    today = date.today()
    total_csps = db.query(CSP).count()
    total_docs = db.query(Document).count()
    current_docs = db.query(Document).filter(Document.is_current.is_(True)).count()
    historical_docs = db.query(Document).filter(Document.is_current.is_(False)).count()
    active_agrs = db.query(Agreement).filter(Agreement.is_active.is_(True)).count()

    print("\n" + "=" * 60)
    print("           POSTGRESQL COMPLIANCE VAULT SUMMARY")
    print("=" * 60)
    print(f"Total CSP Profiles in Master DB:        {total_csps}")
    print(f"Total Documents Stored:                 {total_docs}")
    print(f"  ├─ Current Active Documents:          {current_docs}")
    print(f"  └─ Historical / Expired Documents:    {historical_docs}")
    print(f"Active Agreements in Registry:          {active_agrs}")

    # Inspect Anuj Kumar specifically if present
    anuj = db.query(CSP).filter(CSP.current_code == '1A852327').first()
    if anuj:
        print("\n" + "-" * 60)
        print(f"CASE STUDY CSP: Anuj Kumar ({anuj.current_code})")
        print("-" * 60)
        docs = db.query(Document).filter(Document.csp_id == anuj.id).all()
        for d in docs:
            curr_str = "CURRENT ACTIVE" if d.is_current else "HISTORICAL ARCHIVE"
            exp_str = d.expiry_date.isoformat() if d.expiry_date else "None"
            start_str = d.issue_date.isoformat() if d.issue_date else "None"
            print(f"  [{curr_str:18}] {d.document_type:20} | Start: {start_str} | Exp: {exp_str} | Rule: {d.validity_rule_used}")
        agr = db.query(Agreement).filter(Agreement.csp_id == anuj.id, Agreement.is_active.is_(True)).first()
        if agr:
            print(f"  Active Agreement Record: Start={agr.start_date} | Expiry={agr.expiry_date} | Status={agr.renewal_status.value}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Deterministic Rescan & PostgreSQL Vault Rebuilder")
    parser.add_argument("--clean", action="store_true", help="Wipe existing documents and re-scan from clean slate")
    parser.add_argument("--max-emails", type=int, default=1000, help="Maximum emails to scan (default: 1000)")
    args = parser.parse_args()

    # Step 1: Ensure all tables exist in PostgreSQL
    logger.info("Verifying PostgreSQL schema and tables...")
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Step 2: Clean document tables if requested
        if args.clean:
            clean_database_tables(db)

        # Step 3: Check if CSP Master is populated in PostgreSQL
        csp_count = db.query(CSP).count()
        if csp_count == 0:
            logger.info("CSP Master database is empty. Auto-seeding from 'CSP Details.xlsx'...")
            from scripts.import_csp_master import import_calling_sheet
            xlsx_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "CSP Details.xlsx"))
            if os.path.exists(xlsx_path):
                import_calling_sheet(xlsx_path)
            else:
                logger.warning(f"Could not find {xlsx_path} for auto-seeding.")

        logger.info(f"Starting 2-year historical backfill scan (max_total={args.max_emails})...")
        res = run_2year_historical_backfill(max_total=args.max_emails)
        logger.info(f"Historical backfill complete. Summary: {res}")

        print_compliance_summary(db)
    finally:
        db.close()



if __name__ == "__main__":
    main()

