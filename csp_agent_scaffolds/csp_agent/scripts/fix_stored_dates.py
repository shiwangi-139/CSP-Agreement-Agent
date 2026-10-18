"""
Read every stored agreement and police certificate again with the current
rules and fix the ones read wrongly when they arrived (older rules, or a
line OCR lost that the focused re-read now finds), e.g. an agreement saved
as "expired" by a 2-year guess although it says "valid upto 10/01/2027".

What happens to each current document (no AI model, one file at a time):
  corrected     the new reading is clear and only the validity changes: the
                same issue date (within 3 days) as stored, a validity that is
                printed or stated in the document, nothing for a person to
                check. Its dates are updated.
  to review     the new reading differs but is not clear (another issue
                date, a default validity, a conflict): the document goes to
                the Review queue with both readings; nobody is told anything.
  kept          dates a person entered or confirmed are never changed.
Nothing else changes, and no CSP is messaged unless --message is given.

    python -m scripts.fix_stored_dates                     # dry run: what would change
    python -m scripts.fix_stored_dates --apply
    python -m scripts.fix_stored_dates --csp 1A851828
    python -m scripts.fix_stored_dates --type AGREEMENT --apply

On the shared server, inside tmux:
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.fix_stored_dates
"""
import argparse
from datetime import date

from app import vault
from app.compliance import refresh_category
from app.db import SessionLocal
from app.document_service import _d, _status_for, recompute_current, sync_agreement_row
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from scripts.check_all_documents import read_isolated

TYPES = ("AGREEMENT", "POLICE_VERIFICATION")


def decide(doc: Document, ex: dict) -> tuple[str, str]:
    """('corrected' | 'to review' | 'same' | 'kept' | 'unreadable', why)."""
    if doc.status == DocumentStatus.MANUAL_VERIFIED or (doc.date_source or "") == "MANUAL_REVIEW":
        return "kept", "dates entered by a person"
    if ex.get("timeout") or ex.get("error"):
        return "unreadable", ex.get("error") or "took too long"
    if ex.get("readability") != "READABLE" or ex.get("document_type") != doc.document_type or not ex.get("start_date"):
        return "unreadable", "the current rules can't read it"
    new_issue = _d(ex["start_date"])
    exp = ex.get("expiry_date")
    new_expiry = None if exp == "LIFETIME_NO_EXPIRY" else _d(exp)
    if new_issue == doc.issue_date and new_expiry == doc.expiry_date:
        return "same", ""
    rule = (ex.get("validity_rule_used") or "").upper()
    clear = (not ex.get("type_review") and "DEFAULT" not in rule
             and doc.issue_date is not None and abs((new_issue - doc.issue_date).days) <= 3)
    if clear:
        return "corrected", f"{rule}"
    return "to review", ex.get("type_review") or (
        "another issue date" if doc.issue_date and new_issue != doc.issue_date else f"validity only {rule.lower()}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    ap.add_argument("--type", choices=TYPES)
    ap.add_argument("--message", action="store_true", help="send corrected CSPs the usual WhatsApp update")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    db = SessionLocal()
    try:
        q = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
             .filter(Document.document_type.in_([a.type] if a.type else TYPES), Document.is_current.is_(True),
                     Document.status.in_([DocumentStatus.VALID, DocumentStatus.EXPIRED, DocumentStatus.MANUAL_VERIFIED])))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        rows = q.order_by(Document.document_type, CSP.current_code).all()
        counts, touched = {}, set()
        for i, (doc, csp) in enumerate(rows, 1):
            path = vault.abs_path(doc.storage_path) if doc.storage_path else None
            if path is None or not path.exists():
                counts["file missing"] = counts.get("file missing", 0) + 1
                continue
            ex = read_isolated(path, doc.document_type, a.timeout)
            what, why = decide(doc, ex)
            counts[what] = counts.get(what, 0) + 1
            if what in ("corrected", "to review"):
                print(f"{csp.current_code} {csp.name[:22]:22} {doc.document_type[:6]:6} {doc.issue_date} -> {doc.expiry_date}"
                      f"   now {ex.get('start_date')} -> {ex.get('expiry_date')}   [{what}] {why[:90]}", flush=True)
            if a.apply and what == "corrected":
                old = f"{doc.issue_date} -> {doc.expiry_date} ({doc.validity_rule_used})"
                doc.issue_date, doc.expiry_date = _d(ex["start_date"]), _d(ex.get("expiry_date"))
                doc.validity_rule_used, doc.date_source = ex.get("validity_rule_used"), ex.get("date_source")
                doc.status = _status_for(ex)
                doc.extracted_fields = {**(doc.extracted_fields or {}), "start_date": ex["start_date"],
                                        "expiry_date": ex.get("expiry_date"),
                                        "corrected": f"re-read {date.today().isoformat()}: was {old}"}
                touched.add(csp.id)
            elif a.apply and what == "to review":
                doc.status = DocumentStatus.NEEDS_REVIEW
                db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=(
                    f"Re-read with the current rules: stored {doc.issue_date} -> {doc.expiry_date}, now read "
                    f"{ex.get('start_date')} -> {ex.get('expiry_date')}. {why}. Check the dates on the document.")[:500]))
                touched.add(csp.id)
            if a.apply:
                db.flush()
            if i % 25 == 0:
                print(f"... {i}/{len(rows)}", flush=True)
        if a.apply:
            from app.renewal_engine import on_documents_received
            for cid in touched:
                c = db.get(CSP, cid)
                for t in TYPES:
                    recompute_current(db, c, t)
                sync_agreement_row(db, c)
                vault.place_csp(db, c)
                on_documents_received(db, c)
                refresh_category(db, c)
            db.commit()
            if a.message:
                from app.comms.upload_receipt import send_upload_receipt
                for cid in touched:
                    print(f"  message to {db.get(CSP, cid).current_code}: {send_upload_receipt(cid)}")
        else:
            db.rollback()
        print(f"\n{len(rows)} documents: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
        if not a.apply:
            print("Dry run: nothing changed. Add --apply to save.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
