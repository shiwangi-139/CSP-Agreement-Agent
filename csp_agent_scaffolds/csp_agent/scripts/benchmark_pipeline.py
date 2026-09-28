"""
Time the document pipeline on stored files, as if each CSP folder were one
email with its attachments. Read-only: nothing is stored or changed, and the
vision model is off unless --model is given.

    python -m scripts.benchmark_pipeline --emails 8                  # writes logs/benchmark_*.json
    python -m scripts.benchmark_pipeline --emails 8 --compare logs/benchmark_A.json
                                        # also lists any file whose result changed

Timings are per "email" (all its files) plus totals per OCR stage.
Use --no-cache to measure cold OCR (the default also ignores the cache).
"""
import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

from app import vault

EXTS = (".pdf", ".jpg", ".jpeg", ".png")


def _email_folders(root: Path, n: int) -> list[Path]:
    folders = []
    for f in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        files = [x for x in f.iterdir() if x.is_file() and x.suffix.lower() in EXTS]
        if len(files) >= 2:
            folders.append(f)
        if len(folders) >= n:
            break
    return folders


def _key(r: dict) -> tuple:
    return (r.get("readability"), r.get("document_type"), r.get("start_date"), r.get("expiry_date"),
            r.get("validity_rule_used"), r.get("date_source"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emails", type=int, default=8)
    ap.add_argument("--dir", help="folder of CSP folders (default: the vault, or the old storage/documents)")
    ap.add_argument("--model", action="store_true", help="allow the vision-model fallback (network)")
    ap.add_argument("--use-cache", action="store_true", help="allow the OCR cache (warm timings)")
    ap.add_argument("--sequential", action="store_true", help="one file after another, like the old code")
    ap.add_argument("--compare", help="an earlier benchmark JSON to compare results with")
    args = ap.parse_args()

    from app import ocr_service
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    if hasattr(ocr_service, "set_cache_enabled"):
        ocr_service.set_cache_enabled(args.use_cache)
    extract_all = None
    if not args.sequential:
        try:
            from app.extract_pool import extract_all
        except ImportError:
            extract_all = None

    root = Path(args.dir) if args.dir else (vault.ROOT if vault.ROOT.is_dir() and any(vault.ROOT.iterdir())
                                            else vault.PROJECT_ROOT / "storage" / "documents")
    folders = _email_folders(root, args.emails)
    print(f"{len(folders)} emails from {root}  (mode: {'parallel' if extract_all else 'sequential'})")
    timings = getattr(ocr_service, "TIMINGS", None)
    if timings is not None:
        timings.clear()

    out, per_email = [], []
    for folder in folders:
        files = sorted(x for x in folder.iterdir() if x.is_file() and x.suffix.lower() in EXTS)
        items = [(f.read_bytes(), f.name) for f in files]
        t0 = time.perf_counter()
        if extract_all:
            results = extract_all(items, allow_llm_fallback=args.model)
        else:
            results = [extract_document_fields_deterministic(b, n, allow_llm_fallback=args.model) for b, n in items]
        secs = time.perf_counter() - t0
        per_email.append(secs)
        print(f"  {secs:6.1f}s  {folder.name[:40]:40}  "
              + "  ".join(f"{(r.get('document_type') or '?')[:4]}:{r.get('start_date') or r.get('readability')}"
                          for r in results))
        for f, r in zip(files, results):
            out.append({"file": f"{folder.name}/{f.name}", "pages": r.get("page_count"), **{
                k: r.get(k) for k in ("readability", "document_type", "start_date", "expiry_date",
                                      "validity_rule_used", "date_source", "ocr_method")}})

    if per_email:
        s = sorted(per_email)
        p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
        print(f"per email: p50 {statistics.median(s):.1f}s  p95 {p95:.1f}s  max {s[-1]:.1f}s  "
              f"total {sum(s):.0f}s")
    if timings:
        print("stage totals: " + "  ".join(f"{k} {v:.0f}s" for k, v in sorted(timings.items())))

    logs = vault.PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / f"benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"per_email_seconds": per_email, "stage_totals": dict(timings or {}),
                                "results": out}, indent=1, default=str))
    print(f"saved {path}")

    if args.compare:
        old = {r["file"]: r for r in json.loads(Path(args.compare).read_text())["results"]}
        diffs = [(r, old[r["file"]]) for r in out if r["file"] in old and _key(r) != _key(old[r["file"]])]
        print(f"{len(diffs)} of {len(out)} results differ from {args.compare}")
        for new, prev in diffs:
            print(f"  {new['file']}\n     before {_key(prev)}\n     after  {_key(new)}")


if __name__ == "__main__":
    main()
