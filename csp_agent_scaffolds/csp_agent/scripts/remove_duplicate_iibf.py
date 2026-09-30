"""
A CSP has one IIBF certificate, but it was often sent several times (a new
photo or scan each time), so several copies were stored. Keep one copy per
certificate (same registration number or issue date): the current one,
otherwise the largest file (the clearest scan). New mail already skips such
copies (app/document_service.py: same_iibf).

    python -m scripts.remove_duplicate_iibf            # dry run: list them
    python -m scripts.remove_duplicate_iibf --apply    # move extra copies, remove their records

Extra copies are moved to <vault parent>/legacy/duplicate_iibf/, never deleted.
"""
import argparse
import shutil
from collections import defaultdict

from app import vault
from app.db import SessionLocal
from app.document_service import recompute_current
from app.models import CSP, Document, DocumentStatus, ExtractionCorrection, ManualReviewQueue, OutreachCycle


def extra_copies(db) -> list[tuple[Document, Document]]:
    """(extra copy, the copy that is kept)."""
    by_csp = defaultdict(list)
    for d in db.query(Document).filter(Document.document_type == "IIBF_CERTIFICATE",
                                       Document.readability == "READABLE",
                                       Document.status != DocumentStatus.REJECTED):
        by_csp[d.csp_id].append(d)
    out = []
    for docs in by_csp.values():
        groups: list[list[Document]] = []
        for d in docs:
            g = next((g for g in groups if any(
                (d.iibf_reg_number and x.iibf_reg_number and d.iibf_reg_number.strip() == x.iibf_reg_number.strip())
                or (d.issue_date is not None and d.issue_date == x.issue_date) for x in g)), None)
            (g.append(d) if g is not None else groups.append([d]))
        for g in groups:
            if len(g) < 2:
                continue
            keep = max(g, key=lambda x: (bool(x.is_current), x.file_size_bytes or 0, -(x.id or 0)))
            out += [(x, keep) for x in g if x is not keep]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    dest_dir = vault.ROOT.parent / "legacy" / "duplicate_iibf"
    try:
        rows = extra_copies(db)
        for extra, keep in rows:
            csp = db.get(CSP, extra.csp_id)
            print(f"  {csp.current_code} {csp.name[:24]:24} issued {extra.issue_date}: remove doc={extra.id} "
                  f"({extra.original_filename}), keep doc={keep.id} ({keep.original_filename})")
        print(f"{len(rows)} extra IIBF copies" + ("" if args.apply else " (dry run, nothing changed)"))
        if not args.apply:
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        for extra, keep in rows:
            csp = db.get(CSP, extra.csp_id)
            p = vault.abs_path(extra.storage_path)
            if p is not None and p.exists():
                shutil.move(str(p), str(dest_dir / f"{extra.id}_{p.name}"))
            db.query(ManualReviewQueue).filter(ManualReviewQueue.document_id == extra.id).delete()
            db.query(ExtractionCorrection).filter(ExtractionCorrection.document_id == extra.id).delete()
            db.query(OutreachCycle).filter(OutreachCycle.document_id == extra.id).update({"document_id": keep.id})
            db.delete(extra)
            db.flush()
            recompute_current(db, csp, "IIBF_CERTIFICATE")
            vault.place_csp(db, csp)
            db.commit()
        print(f"moved to {dest_dir}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
