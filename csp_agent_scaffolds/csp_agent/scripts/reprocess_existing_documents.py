"""
scripts/reprocess_existing_documents.py
Re-runs local deterministic extraction on all stored PDF documents in the vault.
Updates agreement dates (3-year Agreement validity, 1-year PV validity) and Neon DB.
"""

import os
from datetime import date
from app.db import SessionLocal
from app.models import CSP, Agreement, Document, RenewalStatus
from sqlalchemy import text
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic, add_years
from app.email_ingest import evaluate_csp_category

def main():
    db = SessionLocal()
    try:
        # Step 0: Ensure database schema allows nullable expiry_date for PV / pending agreements
        try:
            with db.bind.connect() as conn:
                conn.execute(text("ALTER TABLE agreements ALTER COLUMN expiry_date DROP NOT NULL;"))
                conn.commit()
        except Exception:
            pass

        print("=" * 70)
        print("RE-PROCESSING STORED DOCUMENTS WITH NEW DATE & COMPLIANCE RULES")
        print("=" * 70)

        docs = db.query(Document).all()
        print(f"Found {len(docs)} documents in database vault.")

        updated_docs = 0
        for doc in docs:
            if not doc.storage_path or not os.path.exists(doc.storage_path):
                print(f" - Doc #{doc.id}: storage path '{doc.storage_path}' not found on disk. Skipping.")
                continue

            with open(doc.storage_path, "rb") as f:
                pdf_bytes = f.read()

            extracted = extract_document_fields_deterministic(pdf_bytes, os.path.basename(doc.storage_path))
            doc_type = extracted.get("document_type", "UNKNOWN")
            conf = extracted.get("confidence", 0.0)

            # Update document record
            doc.extracted_fields = extracted
            doc.document_type = doc_type
            doc.overall_confidence = conf

            csp = db.query(CSP).filter(CSP.id == doc.csp_id).first()
            agr = db.query(Agreement).filter(Agreement.csp_id == doc.csp_id, Agreement.is_active.is_(True)).first()

            if doc_type == "AGREEMENT":
                exp_val = extracted.get("expiry_date")
                start_val = extracted.get("start_date")
                exp_d = date.fromisoformat(exp_val) if exp_val and exp_val != "LIFETIME_NO_EXPIRY" else None
                start_d = date.fromisoformat(start_val) if start_val else None

                if not exp_d and start_d:
                    exp_d = add_years(start_d, 3)

                if not agr and csp:
                    agr = Agreement(
                        csp_id=csp.id,
                        is_active=True,
                        renewal_status=RenewalStatus.ACTIVE,
                        expiry_date=exp_d,
                        start_date=start_d,
                        current_csp_code=csp.current_code or csp.lookup_code,
                        pdf_hash=doc.sha256,
                        pdf_link=doc.storage_path
                    )
                    db.add(agr)
                    db.flush()
                elif agr:
                    if exp_d: agr.expiry_date = exp_d
                    if start_d: agr.start_date = start_d
                    agr.pdf_hash = doc.sha256
                    agr.pdf_link = doc.storage_path
                if agr: doc.agreement_id = agr.id

            elif doc_type == "POLICE_VERIFICATION":
                exp_val = extracted.get("expiry_date")
                pv_d = date.fromisoformat(exp_val) if exp_val and exp_val != "LIFETIME_NO_EXPIRY" else None
                if not agr and csp:
                    agr = Agreement(
                        csp_id=csp.id,
                        is_active=True,
                        renewal_status=RenewalStatus.ACTIVE,
                        current_csp_code=csp.current_code or csp.lookup_code,
                        expiry_date=None,
                        police_verification_expiry=pv_d
                    )
                    db.add(agr)
                    db.flush()
                elif agr and pv_d:
                    agr.police_verification_expiry = pv_d
                if agr: doc.agreement_id = agr.id

            elif doc_type in ("IIBF_CERTIFICATE", "ANNEXURE_OR_OTHER"):
                if not agr and csp:
                    agr = Agreement(
                        csp_id=csp.id,
                        is_active=True,
                        renewal_status=RenewalStatus.ACTIVE,
                        current_csp_code=csp.current_code or csp.lookup_code,
                        expiry_date=None
                    )
                    db.add(agr)
                    db.flush()
                if agr: doc.agreement_id = agr.id

            updated_docs += 1
            print(f" -> Doc #{doc.id}: Type={doc_type} | Start={extracted.get('start_date')} | Exp={extracted.get('expiry_date')} | Conf={conf}")

        db.commit()
        print(f"\nSuccessfully reprocessed and updated {updated_docs} documents in Neon DB!")

        # Print Category breakdown
        from collections import defaultdict
        csps = db.query(CSP).all()
        categories = {"CATEGORY_A": [], "CATEGORY_B": [], "CATEGORY_C": [], "CATEGORY_D": []}
        all_agrs = {a.csp_id: a for a in db.query(Agreement).filter(Agreement.is_active.is_(True)).all()}
        all_docs = defaultdict(list)
        for d in db.query(Document).all():
            all_docs[d.csp_id].append(d)

        for c in csps:
            active_agr = all_agrs.get(c.id)
            d_list = all_docs.get(c.id, [])
            cat = evaluate_csp_category(c, active_agr, d_list)
            categories[cat].append(c)

        print("\n" + "=" * 70)
        print("UPDATED 4-CATEGORY COMPLIANCE BREAKDOWN")
        print("=" * 70)
        print(f"Total CSPs Tracked     : {len(csps)}")
        print(f"Category A (Compliant)   : {len(categories['CATEGORY_A'])}")
        print(f"Category B (Incomplete)  : {len(categories['CATEGORY_B'])}")
        print(f"Category C (Expired)     : {len(categories['CATEGORY_C'])}")
        print(f"Category D (No Response) : {len(categories['CATEGORY_D'])}")
        print("=" * 70)

    finally:
        db.close()

if __name__ == "__main__":
    main()

