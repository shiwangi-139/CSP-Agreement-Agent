"""
Take e-mail signature logos (tiny images such as image002.jpg or
Outlook-xxxx.png that were stored as "unreadable" documents) out of the
CSP folders. New mail skips them already (app/gmail_ingest.py:
is_signature_image).

    python -m scripts.remove_signature_images            # dry run: list them
    python -m scripts.remove_signature_images --apply    # move files, remove the records

Files are moved to <vault parent>/legacy/signature_images/, never deleted.
Only UNREADABLE records are touched: nothing that counts as a document.
"""
import argparse
import shutil

from app import vault
from app.db import SessionLocal
from app.gmail_ingest import is_signature_image
from app.models import CSP, Document, ExtractionCorrection, ManualReviewQueue, OutreachCycle


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    dest_dir = vault.ROOT.parent / "legacy" / "signature_images"
    try:
        found = []
        for d in db.query(Document).filter(Document.readability == "UNREADABLE",
                                           Document.mime_type.in_(("image/jpeg", "image/png"))):
            p = vault.abs_path(d.storage_path)
            if p is not None and p.exists() and is_signature_image(p.read_bytes()):
                found.append((d, p))
        for d, p in found:
            csp = db.get(CSP, d.csp_id)
            print(f"  {csp.current_code} doc={d.id} {d.original_filename} ({d.file_size_bytes} bytes)")
        print(f"{len(found)} signature images" + ("" if args.apply else " (dry run, nothing changed)"))
        if not args.apply:
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        for d, p in found:
            target = dest_dir / f"{d.id}_{p.name}"
            shutil.move(str(p), str(target))
            db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == d.id).delete()
            db.query(ExtractionCorrection).filter(ExtractionCorrection.document_id == d.id).delete()
            db.query(OutreachCycle).filter(OutreachCycle.document_id == d.id).update({"document_id": None})
            db.delete(d)
            db.commit()
        print(f"moved to {dest_dir}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
