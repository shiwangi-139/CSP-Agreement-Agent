"""
Measure extraction accuracy against documents you have checked by hand.

1. Make samples/gold.csv with one row per file you've checked:
       path,document_type,issue_date,expiry_date
       storage/documents/1A850004_KAUSHAR_JAHAN/AGREEMENT_2024-05-25.pdf,AGREEMENT,2024-05-25,2027-05-25
   (dates YYYY-MM-DD; leave expiry_date empty for IIBF)
2. python -m scripts.eval_extraction [--no-model]

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
    args = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    rows = list(csv.DictReader(open(args.gold, encoding="utf-8")))
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


if __name__ == "__main__":
    main()
