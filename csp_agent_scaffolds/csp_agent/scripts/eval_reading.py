"""
How well does the reader do on documents a person has already decided?
Read-only: nothing in the database changes, no message is sent, the AI
model is never asked.

The correct answers come from the Review queue: a document a person
accepted (its issue and expiry dates as accepted or corrected) or rejected.
Every new review decision adds to the set. A fixed fifth of them (document
id divisible by 5) is the HOLD-OUT set: it is left out by default and only
checked with --holdout, so rules are never tuned to it.

    python -m scripts.eval_reading                 # the tuning set
    python -m scripts.eval_reading --holdout       # the hold-out set
    python -m scripts.eval_reading --type AGREEMENT
    python -m scripts.eval_reading --shadow        # by scripts/shadow_test.py: candidate code, saved apart

On the shared server, inside tmux:
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.eval_reading

What each document counts as:
  correct          accepted on its own with the right dates
  safe review      sent to a person (correct to be careful)
  WRONG ACCEPT     accepted on its own with a wrong date, or a document a
                   person rejected: the dangerous kind, target 0
  wrong reject     turned away (unreadable / not the document / expired)
                   although a person accepted it
Writes logs/reading_eval.csv and saves the totals for the Monitoring page
and the Agent Coach, with one SCORE per document type: 10 x wrong accepts +
1 x wrong rejects (a wrong acceptance is ten times worse), and which
documents were fixed or newly broken since the previous run of the same set.
It also saves the verdict of every document and the results per rule (the
date rule and the validity rule that read it), for the rule health
(app/rule_health.py) and the shadow test (scripts/shadow_test.py).
The worker runs both sets every night at 02:30 (app/worker.py).
"""
import argparse
import csv
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app import vault
from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from scripts.check_all_documents import TYPES, read_isolated


def _truth(db):
    """(document, csp, 'accept' | 'reject') for every document a person decided."""
    rows = (db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
            .join(CSP, CSP.id == Document.csp_id)
            .filter(ManualReviewQueue.status.in_([ReviewStatus.APPROVED, ReviewStatus.CORRECTED, ReviewStatus.REJECTED]))
            .order_by(ManualReviewQueue.resolved_at.desc()).all())
    seen = set()
    for q, d, c in rows:
        if d.id in seen or d.document_type not in TYPES:
            continue
        seen.add(d.id)
        if q.status == ReviewStatus.REJECTED or d.status == DocumentStatus.REJECTED:
            yield d, c, "reject"
        elif d.issue_date:
            yield d, c, "accept"


def judge(truth: str, d: Document, ex: dict) -> str:
    if ex.get("timeout") or ex.get("error"):
        return "could not read"
    read_ok = ex.get("readability") == "READABLE" and ex.get("document_type") == d.document_type
    review = read_ok and (ex.get("type_review") or ex.get("compliance_status") == "NEEDS_REVIEW")
    rejected = not read_ok or ex.get("compliance_status") == "EXPIRED"
    if review:
        return "safe review"
    if truth == "reject":
        return "correct" if rejected else "WRONG ACCEPT"
    if rejected:
        if ex.get("compliance_status") == "EXPIRED" and str(ex.get("start_date")) == str(d.issue_date) \
                and str(ex.get("expiry_date")) == str(d.expiry_date):
            return "correct"               # read right; really expired
        return "wrong reject"
    exp = ex.get("expiry_date") if ex.get("expiry_date") != "LIFETIME_NO_EXPIRY" else None
    same = str(ex.get("start_date")) == str(d.issue_date) and str(exp or None) == str(d.expiry_date or None)
    return "correct" if same else "WRONG ACCEPT"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="check the hold-out set instead")
    ap.add_argument("--type", choices=TYPES)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--shadow", action="store_true",
                    help="candidate code (scripts/shadow_test.py): tuning + hold-out, saved as the 'shadow' set")
    ap.add_argument("--commit", help="the code's commit, when it isn't this folder's")
    a = ap.parse_args()
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        items = [(d, c, t) for d, c, t in _truth(db)
                 if (a.shadow or (d.id % 5 == 0) == a.holdout) and (not a.type or d.document_type == a.type)]
        set_name = "shadow" if a.shadow else "holdout" if a.holdout else "tuning"
        print(f"{len(items)} documents decided by a person ({set_name} set)")
        Path("logs").mkdir(exist_ok=True)
        totals = defaultdict(Counter)
        field_ok = defaultdict(Counter)
        failures = {}                                  # doc id -> verdict, for "fixed / newly broken"
        verdicts = {}                                  # doc id -> verdict, every document
        failure_rules = {}                             # doc id -> the rules that read it, for failures
        by_rule = defaultdict(Counter)                 # rule -> verdicts (date rule and validity rule)
        with open("logs/shadow_eval.csv" if a.shadow else "logs/reading_eval.csv", "w", newline="") as out:
            w = csv.writer(out)
            w.writerow(["csp_code", "type", "doc_id", "truth", "true_issue", "true_expiry", "read_issue", "read_expiry",
                        "rule", "verdict", "note"])
            for i, (d, c, truth) in enumerate(items, 1):
                path = vault.abs_path(d.storage_path) if d.storage_path else None
                if path is None or not path.exists():
                    continue
                ex = read_isolated(path, d.document_type, a.timeout)
                v = judge(truth, d, ex)
                totals[d.document_type][v] += 1
                totals["ALL"][v] += 1
                verdicts[str(d.id)] = v
                for rule in {ex.get("date_source"), ex.get("validity_rule_used")} - {None, ""}:
                    by_rule[rule][v] += 1
                if truth == "accept" and ex.get("start_date"):
                    field_ok[d.document_type]["issue_found"] += 1
                    field_ok[d.document_type]["issue_right"] += str(ex["start_date"]) == str(d.issue_date)
                    field_ok[d.document_type]["expiry_right"] += str(ex.get("expiry_date")) == str(d.expiry_date)
                w.writerow([c.current_code, d.document_type, d.id, truth, d.issue_date, d.expiry_date,
                            ex.get("start_date"), ex.get("expiry_date"), ex.get("validity_rule_used"), v,
                            (ex.get("type_review") or ex.get("rejection_reason") or ex.get("error") or "")[:200]])
                out.flush()
                if v in ("WRONG ACCEPT", "wrong reject"):
                    failures[str(d.id)] = v
                    failure_rules[str(d.id)] = sorted({ex.get("date_source"), ex.get("validity_rule_used")} - {None, ""})
                    print(f"{c.current_code} {d.document_type[:6]:6} doc {d.id}: {v}  person {d.issue_date} -> "
                          f"{d.expiry_date}   read {ex.get('start_date')} -> {ex.get('expiry_date')}", flush=True)
                if i % 25 == 0:
                    print(f"... {i}/{len(items)}", flush=True)
        summary = {}
        for t, cnt in totals.items():
            n = sum(cnt.values())
            f = field_ok.get(t, Counter())
            summary[t] = {"documents": n, **cnt,
                          "correct_or_safe_percent": round(100 * (cnt["correct"] + cnt["safe review"]) / n, 1) if n else None,
                          "wrong_accept_percent": round(100 * cnt["WRONG ACCEPT"] / n, 1) if n else None,
                          "review_percent": round(100 * cnt["safe review"] / n, 1) if n else None,
                          "issue_date_right_when_found": f"{f['issue_right']}/{f['issue_found']}" if f else None,
                          "expiry_right_when_found": f"{f['expiry_right']}/{f['issue_found']}" if f else None,
                          "score": 10 * cnt["WRONG ACCEPT"] + cnt["wrong reject"]}
            print(f"\n{t}: {n} documents  " + ", ".join(f"{k} {cnt[k]}" for k in
                  ("correct", "safe review", "WRONG ACCEPT", "wrong reject", "could not read")))
        try:
            commit = a.commit or subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                    timeout=5).stdout.strip()
        except Exception:
            commit = None
        from app.models import MonitorRun
        from app.monitoring import record_run
        before = next((r.summary or {} for r in db.query(MonitorRun).filter(MonitorRun.kind == "reading_eval")
                       .order_by(MonitorRun.started_at.desc()).limit(10) if (r.summary or {}).get("set") == set_name), {})
        old = before.get("failures") or {}
        fixed = sorted(k for k in old if k not in failures)
        broken = sorted(k for k in failures if k not in old)
        print(f"Score (10 x wrong accepts + wrong rejects): {summary.get('ALL', {}).get('score')}   "
              f"fixed since last run: {len(fixed)}   newly broken: {len(broken)}")
        record_run(db, "reading_eval", started, "OK", {"set": set_name, "commit": commit, "by_type": summary,
                                                        "failures": failures, "fixed": fixed[:200], "broken": broken[:200],
                                                        "verdicts": verdicts, "by_rule": {k: dict(v) for k, v in by_rule.items()},
                                                        "failure_rules": failure_rules,
                                                        "previous_score": (before.get("by_type") or {}).get("ALL", {}).get("score")})
        print("\nFull list: logs/reading_eval.csv   (nothing in the database was changed)")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
