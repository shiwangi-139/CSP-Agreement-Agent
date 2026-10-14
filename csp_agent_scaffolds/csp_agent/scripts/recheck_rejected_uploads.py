"""
Re-check files the upload page turned away as "wrong document" or "expired",
or stored as "unreadable", with the current rules (section hint, more wording, garbage
scanner text layers OCR'd again, review instead of rejection).

For each CSP and document section, the newest turned-away file is read
again. If it is now that document, it is filed exactly as a fresh upload:
accepted, or put in the Review queue for a person when the wording or date
isn't confirmed. CSPs don't need to upload again. A section the CSP has
since uploaded successfully is skipped.

    python -m scripts.recheck_rejected_uploads            # dry run: what would happen, nothing changes
    python -m scripts.recheck_rejected_uploads --apply
    python -m scripts.recheck_rejected_uploads --apply --no-model   # faster: skip the local AI model
    python -m scripts.recheck_rejected_uploads --apply --no-message # don't message the CSPs

With --apply, each CSP whose documents changed gets the usual WhatsApp
reply at once: "all recorded", or what is recorded / being checked / not.
"""
import argparse
from collections import defaultdict

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import USABLE, canonical_type, refresh_category
from app.db import SessionLocal
from app.document_service import _d, _status_for, recompute_current, store_extracted_document, sync_agreement_row
from app.models import CSP, AgreementEvent, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from app.ocr_service import detect_mime

SECTION_TYPE = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}


def _has_good_copy(db, csp_id: int, doc_type: str) -> bool:
    for d in db.query(Document).filter(Document.csp_id == csp_id, Document.is_current.is_(True)):
        if canonical_type(d.document_type) == doc_type and d.status in USABLE and d.readability == "READABLE":
            return True
    return False


def _make_readable(db, csp, doc: Document, ex: dict) -> None:
    """A copy stored as unreadable that reads correctly now: same rules as a
    fresh upload (review when the type or date isn't confirmed)."""
    doc.readability = "READABLE"
    doc.document_type = ex["document_type"]
    doc.issue_date, doc.expiry_date = _d(ex.get("start_date")), _d(ex.get("expiry_date"))
    doc.date_source, doc.validity_rule_used = ex.get("date_source"), ex.get("validity_rule_used")
    doc.validity_months, doc.extraction_method = ex.get("validity_months"), ex.get("extraction_method")
    doc.has_explicit_3year_clause = bool(ex.get("has_explicit_3year_clause"))
    fields = dict(doc.extracted_fields or {}, start_date=ex.get("start_date"), expiry_date=ex.get("expiry_date"),
                  rechecked="re-read with the current rules")
    if ex.get("type_review"):
        fields["needs_confirmation"] = ex["type_review"]
        doc.status = DocumentStatus.NEEDS_REVIEW
        db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=ex["type_review"][:500]))
    else:
        doc.status = _status_for(ex)
    doc.extracted_fields = fields
    db.flush()
    recompute_current(db, csp, doc.document_type)
    sync_agreement_row(db, csp)
    vault.place_csp(db, csp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-model", action="store_true", help="don't ask the local AI model (faster)")
    ap.add_argument("--no-message", action="store_true", help="don't send the CSPs their WhatsApp reply")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        # Newest turned-away file per (CSP, section).
        todo = {}
        for e in (db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD")
                  .order_by(AgreementEvent.sent_at.desc())):
            for i, r in enumerate((e.payload or {}).get("results") or []):
                key = (e.csp_id, r.get("section"))
                # "expired": read with older rules (e.g. a printed end date missed); a
                # file that really is expired stays turned away.
                kept = r.get("reason") in ("wrong_doc", "expired") and r.get("kept_file")
                stored_unreadable = r.get("reason") == "unreadable" and r.get("document_id")
                if (kept or stored_unreadable) and not r.get("rechecked") \
                        and r.get("section") in SECTION_TYPE and key not in todo:
                    todo[key] = (e, i, r)
        counts = defaultdict(int)
        touched = {}                                 # csp_id -> results for the WhatsApp reply
        for (csp_id, section), (e, i, r) in sorted(todo.items(), key=lambda kv: kv[0]):
            csp = db.get(CSP, csp_id)
            doc_type = SECTION_TYPE[section]
            label = f"{csp.current_code} {csp.name[:24]:24} {section:9}"
            if _has_good_copy(db, csp_id, doc_type):
                counts["already has a good copy"] += 1
                print(f"{label} skip: a good copy is already on file")
                continue
            doc = db.get(Document, r["document_id"]) if r.get("document_id") else None
            if doc is not None and doc.readability != "UNREADABLE":
                counts["already re-read"] += 1
                continue
            path = vault.abs_path(doc.storage_path if doc is not None else r["kept_file"])
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
            filed_id = None
            if a.apply and outcome.startswith("->") and doc is not None:
                _make_readable(db, csp, doc, ex)                     # the stored "unreadable" copy, updated in place
                filed_id = doc.id
            elif a.apply and outcome.startswith("->"):
                out = store_extracted_document(db, csp, data, r.get("filename") or path.name,
                                               detect_mime(data) or "application/pdf", ex,
                                               channel="CSP_UPLOAD_PORTAL", sender_on_sheet=True)
                filed_id = out.document.id if out.document is not None else None
            if filed_id:
                touched.setdefault(csp_id, []).append(
                    {"section": section, "ok": True, "reason": "received_check" if ex.get("type_review") else "accepted"})
            if a.apply:
                p = dict(e.payload)
                results = [dict(x) for x in p["results"]]
                results[i]["rechecked"] = outcome[:120]
                if filed_id:
                    results[i]["rechecked_document_id"] = filed_id      # Form uploads shows its state now
                p["results"] = results
                e.payload = p
        if a.apply:
            from app.renewal_engine import on_documents_received
            for cid in touched:
                on_documents_received(db, db.get(CSP, cid))
                refresh_category(db, db.get(CSP, cid))
            db.commit()
            if not a.no_message:
                from app.comms.upload_receipt import send_upload_receipt
                for cid, res in touched.items():
                    print(f"  message to {db.get(CSP, cid).current_code}: {send_upload_receipt(cid, res)}")
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
