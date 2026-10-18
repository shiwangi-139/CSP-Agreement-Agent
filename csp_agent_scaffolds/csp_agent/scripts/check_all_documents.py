"""
Re-read every accepted document (agreement, police certificate, IIBF) with
the current rules and list where the result differs from what is stored.
Read-only: nothing in the database changes and no message is sent. The AI
model is never asked (rules and OCR only), and OCR runs one file at a time.

    python -m scripts.check_all_documents                    # all three types
    python -m scripts.check_all_documents --type AGREEMENT   # one type
    python -m scripts.check_all_documents --csp 1A850249     # one CSP

Run it gently on the shared server, inside tmux so a dropped SSH session
doesn't stop it:
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.check_all_documents
    ... --resume        continue after the documents already in the CSV

Each document is read in its own short-lived process with a time limit
(--timeout, default 180 s): a file that hangs OCR or eats memory is listed
as "timeout" and the check goes on.

Writes the full list to logs/document_check.csv. On screen: only the rows
that are not "same".
  DIFFERENT       the stored dates differ from what the rules read now
  review          the rules aren't sure (a person should look)
  reads as X      the rules now read it as another document (or a fee receipt)
  not readable    the rules can't read it now (stored dates are kept)
  timeout         reading took longer than --timeout
  file missing    the stored file is gone
"""
import argparse
import csv
import multiprocessing as mp
from pathlib import Path

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus

TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")
ACCEPTED = (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED, DocumentStatus.EXPIRED)


def _read(path: str, doc_type: str, q) -> None:
    try:
        ex = extract_document_fields_deterministic(Path(path).read_bytes(), Path(path).name,
                                                   allow_llm_fallback=False, expected_type=doc_type)
        q.put({k: ex.get(k) for k in ("readability", "document_type", "start_date", "expiry_date",
                                      "validity_rule_used", "type_review", "validity_conflict")})
    except Exception as e:                                       # reported, the check goes on
        q.put({"error": f"{type(e).__name__}: {e}"[:200]})


def read_isolated(path: Path, doc_type: str, timeout: int) -> dict:
    """Read one file in a child process: a hang or a memory blow-up stays there."""
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=_read, args=(str(path), doc_type, q), daemon=True)
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.kill()
        p.join()
        return {"timeout": True}
    try:
        return q.get(timeout=5)
    except Exception:
        return {"error": f"reader stopped (exit code {p.exitcode})"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=TYPES, help="only this document type")
    ap.add_argument("--csp", default="", help="only this CSP code")
    ap.add_argument("--timeout", type=int, default=180, help="seconds allowed per document")
    ap.add_argument("--resume", action="store_true", help="skip documents already in logs/document_check.csv")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        q = (db.query(Document, CSP).join(CSP, CSP.id == Document.csp_id)
             .filter(Document.document_type.in_([a.type] if a.type else TYPES),
                     Document.is_current.is_(True), Document.status.in_(ACCEPTED)))
        if a.csp:
            q = q.filter(CSP.current_code == a.csp.strip().upper())
        rows = q.order_by(Document.document_type, CSP.current_code).all()
        Path("logs").mkdir(exist_ok=True)
        csv_path = Path("logs/document_check.csv")
        done = set()
        if a.resume and csv_path.exists():
            done = {int(r["doc_id"]) for r in csv.DictReader(csv_path.open()) if r.get("doc_id", "").isdigit()}
            print(f"resuming: {len(done)} documents already checked")
        counts = {"same": 0, "DIFFERENT": 0, "review": 0, "reads as another": 0, "not readable": 0,
                  "timeout": 0, "file missing": 0}
        with open(csv_path, "a" if done else "w", newline="") as out:
            w = csv.writer(out)
            if not done:
                w.writerow(["csp_code", "name", "type", "doc_id", "status", "stored_issue", "stored_expiry",
                            "stored_rule", "reread_issue", "reread_expiry", "reread_rule", "result", "note"])
            for i, (doc, csp) in enumerate(rows, 1):
                if doc.id in done:
                    continue
                base = [csp.current_code, csp.name, doc.document_type, doc.id, doc.status.value,
                        doc.issue_date, doc.expiry_date, doc.validity_rule_used]
                path = vault.abs_path(doc.storage_path) if doc.storage_path else None
                if path is None or not path.exists():
                    counts["file missing"] += 1
                    w.writerow(base + ["", "", "", "file missing", ""])
                    continue
                ex = read_isolated(path, doc.document_type, a.timeout)
                new_i = ex.get("start_date")
                new_e = ex.get("expiry_date") if ex.get("expiry_date") != "LIFETIME_NO_EXPIRY" else None
                note = " ".join(str(x) for x in (ex.get("type_review"), ex.get("validity_conflict"), ex.get("error"))
                                if x)
                if ex.get("timeout"):
                    res, note = "timeout", f"took over {a.timeout} s ({path.name})"
                elif ex.get("document_type") not in (None, "UNKNOWN", doc.document_type):
                    res, note = "reads as another", f"reads as {ex['document_type']}. {note}"
                elif ex.get("readability") != "READABLE" or ex.get("document_type") != doc.document_type:
                    res = "not readable"
                elif ex.get("type_review") or not new_i:
                    res = "review"
                elif str(doc.issue_date) == str(new_i) and str(doc.expiry_date or None) == str(new_e):
                    res = "same"
                else:
                    res = "DIFFERENT"
                counts[res] += 1
                w.writerow(base + [new_i, new_e, ex.get("validity_rule_used"), res, note])
                out.flush()
                if res != "same":
                    typed = "  (dates entered by a person)" if doc.status == DocumentStatus.MANUAL_VERIFIED else ""
                    print(f"{csp.current_code} {csp.name[:22]:22} {doc.document_type[:6]:6} stored {doc.issue_date} -> "
                          f"{doc.expiry_date}   re-read {new_i} -> {new_e}   [{res}]{typed} {note[:90]}", flush=True)
                if i % 25 == 0:
                    print(f"... {i}/{len(rows)} checked", flush=True)
        print(f"\n{len(rows)} documents checked: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        print("Full list: logs/document_check.csv   (nothing in the database was changed)")
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    main()
