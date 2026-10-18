"""
Re-read every accepted document (agreement, police certificate, IIBF) with
the current rules and list where the result differs from what is stored.
Read-only: nothing in the database changes and no message is sent. The AI
model is never asked (rules and OCR only), and OCR runs one file at a time.

    python -m scripts.check_all_documents                    # all three types
    python -m scripts.check_all_documents --type AGREEMENT   # one type
    python -m scripts.check_all_documents --csp 1A850249     # one CSP

Run it gently on the shared server:
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.check_all_documents

Writes the full list to logs/document_check.csv. On screen: only the rows
that are not "same".
  DIFFERENT       the stored dates differ from what the rules read now
  review          the rules aren't sure (a person should look)
  not readable    the rules can't read it now (stored dates are kept)
  file missing    the stored file is gone
"""
import argparse
import csv
from pathlib import Path

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.db import SessionLocal
from app.models import CSP, Document, DocumentStatus

TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")
ACCEPTED = (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED, DocumentStatus.EXPIRED)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=TYPES, help="only this document type")
    ap.add_argument("--csp", default="", help="only this CSP code")
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
        counts = {"same": 0, "DIFFERENT": 0, "review": 0, "not readable": 0, "file missing": 0}
        with open("logs/document_check.csv", "w", newline="") as out:
            w = csv.writer(out)
            w.writerow(["csp_code", "name", "type", "doc_id", "status", "stored_issue", "stored_expiry", "stored_rule",
                        "reread_issue", "reread_expiry", "reread_rule", "result", "note"])
            for i, (doc, csp) in enumerate(rows, 1):
                base = [csp.current_code, csp.name, doc.document_type, doc.id, doc.status.value,
                        doc.issue_date, doc.expiry_date, doc.validity_rule_used]
                path = vault.abs_path(doc.storage_path) if doc.storage_path else None
                if path is None or not path.exists():
                    counts["file missing"] += 1
                    w.writerow(base + ["", "", "", "file missing", ""])
                    continue
                ex = extract_document_fields_deterministic(path.read_bytes(), path.name, allow_llm_fallback=False,
                                                           expected_type=doc.document_type)
                new_i = ex.get("start_date")
                new_e = ex.get("expiry_date") if ex.get("expiry_date") != "LIFETIME_NO_EXPIRY" else None
                note = " ".join(str(x) for x in (ex.get("type_review"), ex.get("validity_conflict")) if x)
                if ex.get("readability") != "READABLE" or ex.get("document_type") != doc.document_type:
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
