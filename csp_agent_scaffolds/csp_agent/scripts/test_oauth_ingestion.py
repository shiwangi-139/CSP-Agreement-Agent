"""
scripts/test_oauth_ingestion.py
Runs the OAuth email ingestion pipeline and prints the live 4-Category breakdown.
Command:
    .venv/bin/python -m scripts.test_oauth_ingestion
"""

from collections import defaultdict
from app.email_ingest import run_email_ingestion_sync, evaluate_csp_category
from app.db import SessionLocal
from app.models import CSP, Agreement, Document

def main():
    print("=" * 60)
    print("RUNNING GOOGLE CLOUD OAUTH EMAIL INGESTION...")
    print("=" * 60)

    summary = run_email_ingestion_sync()
    print(f"\nResult: Scanned={summary['scanned']}, Processed={summary['processed']}, Duplicates={summary['skipped_duplicate']}, Errors={summary['errors']}")

    print("\n" + "=" * 60)
    print("COMPUTING 4-CATEGORY COMPLIANCE BREAKDOWN (537 CSPs)")
    print("=" * 60)

    db = SessionLocal()
    try:
        csps = db.query(CSP).all()
        categories = {"CATEGORY_A": [], "CATEGORY_B": [], "CATEGORY_C": [], "CATEGORY_D": []}

        # Batch load active agreements and documents to avoid N+1 queries over Neon pooler
        all_agrs = db.query(Agreement).filter(Agreement.is_active.is_(True)).all()
        agr_map = {a.csp_id: a for a in all_agrs}

        all_docs = db.query(Document).all()
        doc_map = defaultdict(list)
        for d in all_docs:
            doc_map[d.csp_id].append(d)

        for c in csps:
            active_agr = agr_map.get(c.id)
            docs = doc_map.get(c.id, [])
            cat = evaluate_csp_category(c, active_agr, docs)
            categories[cat].append(c)

        print(f"Total CSPs Tracked   : {len(csps)}")
        print(f"Category A (Compliant) : {len(categories['CATEGORY_A'])}")
        print(f"Category B (Incomplete): {len(categories['CATEGORY_B'])}")
        print(f"Category C (Expired)   : {len(categories['CATEGORY_C'])}")
        print(f"Category D (No Response): {len(categories['CATEGORY_D'])}")
        print("=" * 60)

        # Print top 3 samples for each category
        for cat_name, items in categories.items():
            print(f"\nSample {cat_name} (First 3):")
            if not items:
                print("  None")
            for item in items[:3]:
                print(f"  - Code: {item.lookup_code} | Name: {item.name} | Phone: {item.phone or 'N/A'}")

    finally:
        db.close()

if __name__ == "__main__":
    main()

