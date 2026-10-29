"""
Shadow test of a reading-rule change, BEFORE it goes live (feedback loop).

The candidate code (another checkout, e.g. the latest GitHub version) reads
every document a person has decided (tuning + hold-out), with this server's
settings and stored files, and its verdicts are compared with the live
code's latest nightly reading check:

  fixed         wrong before, right with the candidate
  broken        right before, wrong with the candidate (a WRONG ACCEPT here
                means: don't put it live)
  CSPs          the CSPs whose documents change verdict

Read-only for documents and CSPs: nothing is stored on a document, no
message is sent, the AI model is never asked. The result is saved for
Monitoring → Rules & safety ("shadow test"). Putting the change live stays
a person's decision (scripts/update.sh), and the next night's release check
(app/rule_health.py) confirms it.

On the server (as root), the candidate code first, then the test inside tmux
(it reads every decided document: slow):
    /home/shiwangi/csp_agent_scaffolds/csp_agent/scripts/shadow_prepare.sh        # latest on GitHub
    cd /home/shiwangi/csp_agent_scaffolds/csp_agent
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.shadow_test \
        --candidate /home/shiwangi/csp_shadow/csp_agent_scaffolds/csp_agent
"""
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent

RUNNER = """
import os, sys, runpy
from dotenv import load_dotenv
load_dotenv(os.path.join({app!r}, ".env"))     # this server's settings; the candidate only brings code
sys.path.insert(0, {cand!r})
sys.argv = ["eval_reading", "--shadow", "--commit", {commit!r}]
runpy.run_module("scripts.eval_reading", run_name="__main__")
"""


def compare(live: dict, shadow: dict) -> dict:
    """Per-document verdicts of the live code against the candidate's."""
    a, b = live.get("verdicts") or {}, shadow.get("verdicts") or {}
    bad = ("WRONG ACCEPT", "wrong reject")
    common = set(a) & set(b)
    fixed = sorted((k for k in common if a[k] in bad and b[k] not in bad), key=int)
    broken = sorted((k for k in common if a[k] not in bad and b[k] in bad), key=int)
    changed = sorted((k for k in common if a[k] != b[k]), key=int)
    new_wrong_accepts = [k for k in broken if b[k] == "WRONG ACCEPT"]
    verdict = ("do not put live: it accepts documents wrongly that are right today" if new_wrong_accepts else
               "safe to put live" if fixed and not broken else
               "no change in verdicts" if not changed else
               "mixed: look at the broken documents first")
    return {"compared": len(common), "fixed": fixed, "broken": broken, "new_wrong_accepts": new_wrong_accepts,
            "changed": len(changed), "verdict": verdict,
            "live_score": ((live.get("by_type") or {}).get("ALL") or {}).get("score"),
            "shadow_score": ((shadow.get("by_type") or {}).get("ALL") or {}).get("score")}


def _latest(db, set_name):
    from app.models import MonitorRun
    for r in db.query(MonitorRun).filter(MonitorRun.kind == "reading_eval").order_by(MonitorRun.started_at.desc()).limit(30):
        if (r.summary or {}).get("set") == set_name:
            return r.summary or {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, help="folder holding the candidate's app/ and scripts/")
    ap.add_argument("--skip-run", action="store_true", help="only compare the latest shadow run again")
    a = ap.parse_args()
    cand = Path(a.candidate).resolve()
    if not (cand / "app" / "__init__.py").exists() or not (cand / "scripts" / "eval_reading.py").exists():
        sys.exit(f"{cand} has no app/ and scripts/eval_reading.py: give the folder of the candidate's csp_agent.")
    try:
        commit = subprocess.run(["git", "-C", str(cand), "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=5).stdout.strip() or "candidate"
    except Exception:
        commit = "candidate"
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    if not a.skip_run:
        print(f"Reading every decided document with candidate {commit} ({cand}) ...", flush=True)
        r = subprocess.run([sys.executable, "-c", RUNNER.format(app=str(APP_DIR), cand=str(cand), commit=commit)],
                           cwd=str(APP_DIR))
        if r.returncode != 0:
            sys.exit(f"The candidate's reading check stopped (exit {r.returncode}); nothing compared.")
    from app.db import SessionLocal
    from app.monitoring import record_run
    db = SessionLocal()
    try:
        tuning, holdout, shadow = _latest(db, "tuning"), _latest(db, "holdout"), _latest(db, "shadow")
        live = {"verdicts": {**(holdout.get("verdicts") or {}), **(tuning.get("verdicts") or {})},
                "by_type": tuning.get("by_type")}
        if not live["verdicts"]:
            sys.exit("No nightly reading check with per-document verdicts yet (it runs at 02:30): try tomorrow.")
        out = compare(live, shadow)
        out.update(candidate=shadow.get("commit") or commit, live=tuning.get("commit"))
        from app.models import CSP, Document
        ids = [int(k) for k in out["fixed"] + out["broken"]][:200]
        codes = {d.id: c.current_code for d, c in db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
                 .filter(Document.id.in_(ids))} if ids else {}
        out["csps_fixed"] = sorted({codes[int(k)] for k in out["fixed"] if int(k) in codes})[:50]
        out["csps_broken"] = sorted({codes[int(k)] for k in out["broken"] if int(k) in codes})[:50]
        print(f"\nCandidate {out['candidate']} vs live {out['live']}: {out['compared']} documents compared")
        print(f"  fixed {len(out['fixed'])}   broken {len(out['broken'])} (of which accepted wrongly "
              f"{len(out['new_wrong_accepts'])})   verdict changed {out['changed']}")
        print(f"  => {out['verdict']}")
        for k in out["broken"][:20]:
            print(f"     broken: document #{k} ({codes.get(int(k), '?')}) now {shadow['verdicts'][k]}")
        record_run(db, "shadow_test", started, "OK", out)
    finally:
        db.close()


if __name__ == "__main__":
    main()
