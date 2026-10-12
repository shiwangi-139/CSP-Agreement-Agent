"""
Re-check files the upload page turned away as "wrong document", with the
current rules (section hint, more wording, review instead of rejection).

For each CSP and document section, the newest turned-away file is read
again. If it is now that document, it is filed exactly as a fresh upload:
accepted, or put in the Review queue for a person when the wording or date
isn't confirmed. CSPs don't need to upload again. A section the CSP has
since uploaded successfully is skipped.

    python -m scripts.recheck_rejected_uploads            # dry run: what would happen, nothing changes
    python -m scripts.recheck_rejected_uploads --apply
    python -m scripts.recheck_rejected_uploads --apply --no-model   # faster: skip the local AI model
"""
import argparse
from collections import defaultdict

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import USABLE, canonical_type, refresh_category
from app.db import SessionLocal
from app.document_service import store_extracted_document
from app.models import CSP, AgreementEvent, Document
from app.ocr_service import detect_mime

SECTION_TYPE = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}


def _has_good_copy(db, csp_id: int, doc_type: str) -> bool:
    for d in db.query(Document).filter(Document.csp_id == csp_id, Document.is_current.is_(True)):
        if canonical_type(d.document_type) == doc_type and d.status in USABLE and d.readability == "READABLE":
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-model", action="store_true", help="don't ask the local AI model (faster)")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        # Newest turned-away file per (CSP, section).
        todo = {}
        for e in (db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD")
                  .order_by(AgreementEvent.sent_at.desc())):
            for i, r in enumerate((e.payload or {}).get("results") or []):
                key = (e.csp_id, r.get("section"))
                if r.get("reason") == "wrong_doc" and r.get("kept_file") and not r.get("rechecked") \
                        and r.get("section") in SECTION_TYPE and key not in todo:
                    todo[key] = (e, i, r)
        counts = defaultdict(int)
        touched = set()
        for (csp_id, section), (e, i, r) in sorted(todo.items(), key=lambda kv: kv[0]):
            csp = db.get(CSP, csp_id)
            doc_type = SECTION_TYPE[section]
            label = f"{csp.current_code} {csp.name[:24]:24} {section:9}"
            if _has_good_copy(db, csp_id, doc_type):
                counts["already has a good copy"] += 1
                print(f"{label} skip: a good copy is already on file")
                continue
            path = vault.abs_path(r["kept_file"])
            if path is None or not path.exists():
                counts["file missing"] += 1
                print(f"{label} skip: kept file not found")
                continue
            data = path.read_bytes()
            ex = extract_document_fields_deterministic(data, r.get("filename") or path.name,
                                                       allow_llm_fallback=not a.no_model, expected_type=doc_type)
            if ex["readability"] != "READABLE" or ex["document_type"] != doc_type:
                outcome = f"still not accepted ({ex['readability']} / {ex['document_type']})"
            elif ex.get("compliance_status") == "EXPIRED":
                outcome = f"read correctly but EXPIRED ({ex.get('expiry_date')})"
            elif ex.get("type_review"):
                outcome = "-> review queue (a person confirms it)"
            else:
                outcome = f"-> ACCEPTED (issued {ex.get('start_date')}, valid till {ex.get('expiry_date')})"
            counts[outcome.split(" (")[0]] += 1
            print(f"{label} {outcome}")
            if a.apply and outcome.startswith("->"):
                store_extracted_document(db, csp, data, r.get("filename") or path.name,
                                         detect_mime(data) or "application/pdf", ex,
                                         channel="CSP_UPLOAD_PORTAL", sender_on_sheet=True)
                touched.add(csp_id)
            if a.apply:
                p = dict(e.payload)
                results = [dict(x) for x in p["results"]]
                results[i]["rechecked"] = outcome[:120]
                p["results"] = results
                e.payload = p
        if a.apply:
            from app.renewal_engine import on_documents_received
            for cid in touched:
                on_documents_received(db, db.get(CSP, cid))
                refresh_category(db, db.get(CSP, cid))
            db.commit()
        else:
            db.rollback()
        print("\nSummary:")
        for k, n in sorted(counts.items()):
            print(f"  {n:4}  {k}")
        print("applied." if a.apply else "dry run: nothing changed. Run with --apply to file them.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
