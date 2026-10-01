"""
Add a balanced sample of stored documents to the dashboard's Review queue as
"Reference check", for a one-time check by a person. Their answers measure
the agent's accuracy (dashboard: Accuracy) and become the reference set that
scripts/eval_extraction.py --from-reviews re-reads after every change.

    python -m scripts.build_reference_set              # dry run: shows the sample
    python -m scripts.build_reference_set --apply      # add it to the Review queue
    python -m scripts.build_reference_set --size 300 --apply

The sample is spread evenly over document type (Agreement, PVR, IIBF) and
how the document was read (PDF text, scanned PDF, photo, vision model), so
hard cases such as phone photos are not drowned out by clean PDFs. Documents
already checked are skipped. Nothing about the documents themselves changes.
"""
import argparse
import random
from collections import defaultdict

from app import accuracy
from app.db import SessionLocal
from app.models import Document, DocumentStatus, ManualReviewQueue

TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")


def sample(db, size: int, seed: int = 7) -> list[Document]:
    checked = {i for (i,) in db.query(ManualReviewQueue.document_id)}
    groups: dict[tuple, list[Document]] = defaultdict(list)
    for d in db.query(Document).filter(Document.readability == "READABLE", Document.document_type.in_(TYPES),
                                       Document.status != DocumentStatus.REJECTED):
        if d.id not in checked:
            groups[(d.document_type, accuracy.how_read(d))].append(d)
    rng = random.Random(seed)
    for g in groups.values():
        rng.shuffle(g)
    # Round-robin over the groups: equal shares, small groups taken whole.
    out, keys = [], sorted(groups)
    while len(out) < size and any(groups[k] for k in keys):
        for k in keys:
            if groups[k] and len(out) < size:
                out.append(groups[k].pop())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=200)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        picked = sample(db, args.size)
        counts = defaultdict(int)
        for d in picked:
            counts[(d.document_type, accuracy.how_read(d))] += 1
        for (t, how), n in sorted(counts.items()):
            print(f"  {t:20} {how:20} {n}")
        print(f"{len(picked)} documents in the reference sample" + ("" if args.apply else " (dry run)"))
        if args.apply:
            added = sum(accuracy.add_reference(db, d) for d in picked)
            db.commit()
            print(f"added {added} reference checks to the Review queue")
    finally:
        db.close()


if __name__ == "__main__":
    main()
