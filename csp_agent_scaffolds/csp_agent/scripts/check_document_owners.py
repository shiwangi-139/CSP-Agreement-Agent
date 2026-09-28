"""
Find Gmail documents filed under the wrong CSP (the CSP came from the email
subject, but the document shows another CSP's code or name), using the same
rule new mail now follows (app/gmail_ingest.py: check_owner).

    python -m scripts.check_document_owners            # dry run: writes logs/owner_check_*.csv
    python -m scripts.check_document_owners --apply    # act on the plan
    python -m scripts.check_document_owners --model    # also ask the vision model about
                                                        # documents OCR could not confirm (slow)

What --apply does, per document:
  MOVE    shows exactly one other CSP's code: moved to that CSP
  REVIEW  names another CSP, or shows neither code nor name: status
          NEEDS_REVIEW + a row in the dashboard's review queue; it stops
          counting as that CSP's document until a reviewer accepts it
Documents a reviewer already accepted or rejected are left alone.
Reading uses the OCR cache, so it is quick for files read before.
"""
import argparse
import csv
from datetime import datetime
from pathlib import Path

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic, read_owner_with_model
from app.db import SessionLocal
from app.document_service import recompute_current, sync_agreement_row
from app.gmail_ingest import check_owner, learned_names
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus

SKIP = {DocumentStatus.MANUAL_VERIFIED, DocumentStatus.REJECTED}


def plan(db, use_model: bool = False) -> list[dict]:
    everyone = db.query(CSP).all()
    learned = learned_names(db)
    by_id = {c.id: c for c in everyone}
    rows = []
    docs = (db.query(Document).filter(Document.upload_channel == "GMAIL_INBOUND",
                                      Document.readability == "READABLE").order_by(Document.id).all())
    for d in docs:
        if d.status in SKIP or (d.extracted_fields or {}).get("owner_check"):
            continue
        path = vault.abs_path(d.storage_path)
        if path is None or not path.exists():
            continue
        data = path.read_bytes()
        ex = extract_document_fields_deterministic(data, d.original_filename or "", allow_llm_fallback=False)
        csp = by_id[d.csp_id]
        ex = {**ex, "readability": "READABLE"}
        ask = (lambda: read_owner_with_model(data, d.document_type)) if use_model else None
        owner, _, review = check_owner(db, csp, ex, everyone, ask_model=ask, learned=learned)
        if owner.id != csp.id:
            action, reason = "MOVE", f"Document shows {owner.current_code} ({owner.name})."
        elif review:
            action, reason = "REVIEW", review
        else:
            continue
        rows.append({"document_id": d.id, "action": action, "type": d.document_type,
                     "filed_under": f"{csp.current_code} {csp.name}", "move_to": owner.current_code if action == "MOVE" else "",
                     "reason": reason, "file": d.storage_path, "_owner": owner,
                     "_seen": ex.get("owner_seen_name")})
    return rows


def apply(db, rows: list[dict]) -> None:
    for r in rows:
        d = db.get(Document, r["document_id"])
        old = db.get(CSP, d.csp_id)
        if r["action"] == "MOVE":
            d.csp_id, d.agreement_id = r["_owner"].id, None
            db.flush()
            affected = (old, r["_owner"])
        else:
            d.status = DocumentStatus.NEEDS_REVIEW
            d.extracted_fields = {**(d.extracted_fields or {}), "owner_check": r["reason"],
                                  "owner_seen_name": r["_seen"]}
            db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason=r["reason"][:500]))
            affected = (old,)
        for c in affected:
            recompute_current(db, c, d.document_type)
            sync_agreement_row(db, c)
            vault.place_csp(db, c)
        db.commit()
        print(f"{r['action']:6} doc={d.id} {r['filed_under']} -> {r['move_to'] or 'review'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--model", action="store_true", help="ask the vision model when OCR can't confirm the owner")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        rows = plan(db, use_model=args.model)
        out = Path("logs") / f"owner_check_{datetime.now():%Y%m%d_%H%M%S}.csv"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=[k for k in (rows[0] if rows else {"document_id": 0}) if not k.startswith("_")]
                               or ["document_id"], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        moves = sum(r["action"] == "MOVE" for r in rows)
        print(f"{len(rows)} documents need attention: {moves} to move, {len(rows) - moves} to review. Plan: {out}")
        if args.apply:
            apply(db, rows)
        else:
            print("Dry run. Run again with --apply to act on it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
