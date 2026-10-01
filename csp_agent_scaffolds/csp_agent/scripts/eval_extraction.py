"""
Measure extraction accuracy against documents you have checked by hand.

1. Make samples/gold.csv with one row per file you've checked:
       path,document_type,issue_date,expiry_date
       storage/documents/1A850004_KAUSHAR_JAHAN/AGREEMENT_2024-05-25.pdf,AGREEMENT,2024-05-25,2027-05-25
   (dates YYYY-MM-DD; leave expiry_date empty for IIBF)
2. python -m scripts.eval_extraction [--no-model]

Or, instead of a CSV, use every document a person confirmed or corrected in
the dashboard's Review queue (spot checks and reference checks, app/accuracy.py):
    python -m scripts.eval_extraction --from-reviews --no-model
    python -m scripts.eval_extraction --from-reviews --fail-below 90   # exit 1 if worse: run before deploying

Prints per-field accuracy, how each document was read (rules vs model), and
every mismatch so the rules can be improved.
"""
import argparse
import csv
import logging
from collections import Counter
from pathlib import Path

from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default="samples/gold.csv")
    ap.add_argument("--no-model", action="store_true", help="rules only (no Ollama/Groq)")
    ap.add_argument("--from-reviews", action="store_true", help="use documents checked in the Review queue")
    ap.add_argument("--fail-below", type=float, help="exit with status 1 if issue-date accuracy is below this %%")
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    rows = reviewed_rows() if args.from_reviews else list(csv.DictReader(open(args.gold, encoding="utf-8")))
    score, method, misses = Counter(), Counter(), []
    for r in rows:
        data = Path(r["path"]).read_bytes()
        ex = extract_document_fields_deterministic(data, Path(r["path"]).name, allow_llm_fallback=not args.no_model)
        method[ex.get("extraction_method")] += 1
        got = {"document_type": ex.get("document_type"), "issue_date": ex.get("start_date") or "",
               "expiry_date": "" if ex.get("expiry_date") == "LIFETIME_NO_EXPIRY" else (ex.get("expiry_date") or "")}
        for field in ("document_type", "issue_date", "expiry_date"):
            ok = (got[field] or "") == (r.get(field) or "").strip()
            score[field] += ok
            if not ok:
                misses.append((r["path"], field, r.get(field), got[field], ex.get("date_source"), ex.get("readability")))
    n = len(rows) or 1
    print(f"{len(rows)} documents")
    for field in ("document_type", "issue_date", "expiry_date"):
        print(f"  {field:14} {score[field]}/{len(rows)} = {100 * score[field] / n:.1f}%")
    print("read by:", dict(method))
    if misses:
        print("\nmismatches (file, field, expected, got, rule, decision):")
        for m in misses:
            print("  ", *m)
    if args.fail_below is not None and rows and 100 * score["issue_date"] / n < args.fail_below:
        print(f"\nFAILED: issue-date accuracy below {args.fail_below}%")
        raise SystemExit(1)


def reviewed_rows() -> list[dict]:
    """Expected answers from the Review queue: a person accepted the document
    as read (APPROVED) or fixed its date (CORRECTED); the document row now
    holds the right values. Rejected ones are left out (no right answer)."""
    from app import accuracy, vault
    from app.db import SessionLocal
    from app.models import Document, ManualReviewQueue, ReviewStatus
    db = SessionLocal()
    try:
        rows = []
        q = (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
             .filter(ManualReviewQueue.reason.like(f"{accuracy.SPOT}%") | ManualReviewQueue.reason.like(f"{accuracy.REFERENCE}%"),
                     ManualReviewQueue.status.in_((ReviewStatus.APPROVED, ReviewStatus.CORRECTED))))
        for _, d in q:
            p = vault.abs_path(d.storage_path)
            if p is None or not p.exists():
                continue
            rows.append({"path": str(p), "document_type": d.document_type,
                         "issue_date": d.issue_date.isoformat() if d.issue_date else "",
                         "expiry_date": d.expiry_date.isoformat() if d.expiry_date else ""})
        return rows
    finally:
        db.close()


if __name__ == "__main__":
    main()
