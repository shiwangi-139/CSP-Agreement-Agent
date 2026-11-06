"""
Documents saved as another type than their file name says (app/type_hint.py):
"CSP Agreement Sunita KO 1A852309.pdf" counted as her police certificate,
"PVR 1A851336.pdf" as an IIBF certificate, Mathura Prasad's agreement as an
expired PVR. Each is read again as the type its file name says:

  RETYPE  it reads as that type: corrected in place and counted as one (a spot
          check in the Review queue shows the type was corrected)
  REVIEW  it doesn't (or a person had accepted it as the other type): a Review
          item asks a person to check the type; one counted as the other type
          stops counting until then
  SKIP    still unreadable: left as it is

    sudo .venv/bin/python -m scripts.fix_type_from_filename                 # dry run: list, change nothing
    sudo .venv/bin/python -m scripts.fix_type_from_filename --csp 1A850853
    sudo .venv/bin/python -m scripts.fix_type_from_filename --apply

sudo: it reads the files and puts them in their right folders (they belong
to the app). No message is sent.
"""
import argparse
from collections import Counter
from datetime import date

from app import vault
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.compliance import canonical_type
from app.db import SessionLocal
from app.document_service import recompute_current, store_extracted_document, sync_agreement_row
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus
from app.type_hint import label, type_named_in


def plan(db, only: str = ""):
    q = db.query(Document).filter(Document.status != DocumentStatus.REJECTED, Document.original_filename.isnot(None))
    for d in q.order_by(Document.id).all():
        named, stored = type_named_in(d.original_filename), canonical_type(d.document_type)
        if not named or named == stored:
            continue
        csp = db.get(CSP, d.csp_id)
        if only and csp.current_code != only.strip().upper():
            continue
        path = vault.abs_path(d.storage_path)
        if path is None or not path.exists():
            yield {"doc": d, "csp": csp, "named": named, "stored": stored, "action": "SKIP", "why": "file not found"}
            continue
        data = path.read_bytes()
        ex = extract_document_fields_deterministic(data, d.original_filename, expected_type=named, allow_llm_fallback=False)
        reads = (ex.get("readability") == "READABLE" and canonical_type(ex.get("document_type")) == named
                 and not ex.get("type_review"))
        if reads and d.status not in (DocumentStatus.MANUAL_VERIFIED,):
            action, why = "RETYPE", f"reads as {label(named)} (issued {ex.get('start_date') or '?'})"
        elif d.readability == "UNREADABLE" and not reads:
            action, why = "SKIP", "still unreadable"
        else:
            action = "REVIEW"
            why = ("a person accepted it as " + label(stored)) if d.status == DocumentStatus.MANUAL_VERIFIED \
                else f"doesn't read as {label(named)} (reads as {label(ex.get('document_type'))})"
        yield {"doc": d, "csp": csp, "named": named, "stored": stored, "action": action, "why": why,
               "data": data, "ex": ex}


def apply_row(db, r) -> None:
    d, csp, named, stored = r["doc"], r["csp"], r["named"], r["stored"]
    if r["action"] == "RETYPE":
        note = (f"Saved as {label(stored)} when it arrived ({d.upload_channel}); its file name says {label(named)} "
                f"and it reads as one: corrected on {date.today():%d-%m-%Y} (type check by file name).")
        out = store_extracted_document(db, csp, r["data"], d.original_filename, d.mime_type, r["ex"],
                                       channel=d.upload_channel or "", expected_type=named, retype_note=note)
        assert out.document is not None and out.document.id == d.id, out
    elif r["action"] == "REVIEW":
        reason = (f"The file name ({d.original_filename}) says {label(named)}, but it is saved as {label(stored)} "
                  f"({r['why']}). Check the type: choose the right one in Type and accept, or reject it.")[:500]
        if not db.query(ManualReviewQueue.id).filter(ManualReviewQueue.document_id == d.id,
                                                     ManualReviewQueue.status == ReviewStatus.PENDING).first():
            db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason=reason))
        if d.is_current:
            # Counted as the other type: not any more until a person decides.
            d.status = DocumentStatus.NEEDS_REVIEW
            d.extracted_fields = {**(d.extracted_fields or {}), "needs_confirmation": reason}
            db.flush()
            recompute_current(db, csp, stored)
            sync_agreement_row(db, csp)
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csp", default="")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        n = Counter()
        counted = 0
        for r in list(plan(db, a.csp)):
            d = r["doc"]
            n[r["action"]] += 1
            counted += bool(d.is_current and r["action"] != "SKIP")
            print(f"  {r['action']:6} doc {d.id:<6} {r['csp'].current_code} {(r['csp'].name or '')[:24]:24} saved as "
                  f"{label(r['stored'])[:22]:22} file says {label(r['named'])[:22]:22} {'COUNTED ' if d.is_current else ''}"
                  f"{r['why']}  ({d.original_filename})")
            if a.apply and r["action"] != "SKIP":
                apply_row(db, r)
        print(f"\n{sum(n.values())} document(s): {n['RETYPE']} to correct, {n['REVIEW']} for a person, {n['SKIP']} left "
              f"as they are; {counted} counted now as the type they are saved as.")
        if not a.apply:
            db.rollback()
            print("Dry run: nothing changed. Run again with --apply (with sudo).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
