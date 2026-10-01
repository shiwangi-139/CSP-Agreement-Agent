"""
app/accuracy.py
How accurate is the agent? Measured from people's answers in the dashboard's
Review queue, so nobody has to re-check every document.

Two kinds of check land in the Review queue next to genuine conflicts:
  Spot check       SPOT_CHECK_RATE (5%) of documents the agent accepted on
                   its own, picked at random when they are stored. Their
                   status is NOT changed: they keep counting as usual.
  Reference check  a one-off, balanced sample of stored documents (by type
                   and by how they were read), added by
                   scripts/build_reference_set.py.

A reviewer's answer is the truth for that document:
  Accept                  everything was read correctly
  Accept + changed date   the issue date was wrong
  Reject                  wrong document, or the wrong CSP

stats() turns those answers into accuracy figures (dashboard: Accuracy), and
scripts/eval_extraction.py --from-reviews re-reads the same files to check
that a code change does not make the agent worse.
"""
import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .config import SPOT_CHECK_RATE
from .models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus

SPOT = "Spot check"
REFERENCE = "Reference check"
CHECK_TEXT = "confirm the document type, the CSP and the issue date were read correctly."
AUTO_ACCEPTED = {DocumentStatus.VALID, DocumentStatus.EXPIRED}


def _picked(sha256: str, rate: float) -> bool:
    """Random but repeatable: the same file is always picked, or never."""
    return int(hashlib.sha256(f"spot:{sha256}".encode()).hexdigest()[:8], 16) % 10_000 < rate * 10_000


def maybe_spot_check(db: Session, doc: Document, rate: Optional[float] = None) -> bool:
    """Called when a document is stored. Adds a spot check for a share of the
    documents the agent accepted without any doubt."""
    rate = SPOT_CHECK_RATE if rate is None else rate
    if rate <= 0 or doc.readability != "READABLE" or doc.status not in AUTO_ACCEPTED or not doc.sha256:
        return False
    if not _picked(doc.sha256, rate):
        return False
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=f"{SPOT}: {CHECK_TEXT}"))
    return True


def add_reference(db: Session, doc: Document) -> bool:
    if db.query(ManualReviewQueue.id).filter(ManualReviewQueue.document_id == doc.id,
                                             ManualReviewQueue.reason.like(f"{REFERENCE}%")).first():
        return False
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason=f"{REFERENCE}: {CHECK_TEXT}"))
    return True


def how_read(doc: Document) -> str:
    """Text layer / OCR / vision model / photo, for accuracy per reading route."""
    method = (doc.extraction_method or "").upper()
    if "MODEL" in method or (doc.date_source or "").startswith("MODEL_VISION"):
        return "vision model"
    if (doc.mime_type or "").startswith("image/"):
        return "photo (OCR)"
    if "DIGITAL" in method or "PYMUPDF" in method:
        return "PDF text"
    return "scanned PDF (OCR)"


def _verdict(q: ManualReviewQueue) -> Optional[str]:
    if q.status == ReviewStatus.APPROVED:
        return "correct"
    if q.status == ReviewStatus.CORRECTED:
        return "date wrong"
    if q.status == ReviewStatus.REJECTED:
        return "wrong document or CSP"
    return None


def stats(db: Session, days: int = 90) -> dict:
    """Accuracy from resolved spot and reference checks, plus how documents
    were read and how often the agent needed help, over the last `days`."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    checks = (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
              .filter(ManualReviewQueue.reason.like(f"{SPOT}%") | ManualReviewQueue.reason.like(f"{REFERENCE}%"))
              .all())
    done = [(q, d) for q, d in checks if _verdict(q)]
    pending = sum(1 for q, _ in checks if q.status == ReviewStatus.PENDING)

    def rate(rows) -> dict:
        n = len(rows)
        v = Counter(_verdict(q) for q, _ in rows)
        return {"checked": n, "correct": v["correct"], "date_wrong": v["date wrong"],
                "wrong_document": v["wrong document or CSP"],
                "accuracy": round(100 * v["correct"] / n, 1) if n else None}

    by_type = {t: rate([(q, d) for q, d in done if d.document_type == t])
               for t in ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")}
    routes = sorted({how_read(d) for _, d in done})
    by_route = {r: rate([(q, d) for q, d in done if how_read(d) == r]) for r in routes}

    recent = db.query(Document).filter(Document.uploaded_at >= since).all()
    reading = Counter(how_read(d) for d in recent if d.readability == "READABLE")
    outcome = Counter("unreadable" if d.readability == "UNREADABLE"
                      else "held for review" if d.status == DocumentStatus.NEEDS_REVIEW
                      else "needs approval" if d.status == DocumentStatus.NEEDS_APPROVAL
                      else "accepted automatically" for d in recent)
    total = len(recent)
    return {
        "overall": {**rate(done), "pending": pending},
        "by_type": by_type,
        "by_route": by_route,
        "recent_days": days,
        "recent_documents": total,
        "recent_outcome": {k: {"count": v, "share": round(100 * v / total, 1)} for k, v in outcome.most_common()},
        "recent_reading": dict(reading.most_common()),
        "spot_check_rate": SPOT_CHECK_RATE,
    }


def mistakes(db: Session, limit: int = 50) -> list[dict]:
    """The latest checks a reviewer had to correct or reject: what to improve."""
    rows = (db.query(ManualReviewQueue, Document, CSP).join(Document, Document.id == ManualReviewQueue.document_id)
            .join(CSP, CSP.id == Document.csp_id)
            .filter(ManualReviewQueue.reason.like(f"{SPOT}%") | ManualReviewQueue.reason.like(f"{REFERENCE}%"),
                    ManualReviewQueue.status.in_((ReviewStatus.CORRECTED, ReviewStatus.REJECTED)))
            .order_by(ManualReviewQueue.resolved_at.desc().nullslast()).limit(limit).all())
    return [{"document_id": d.id, "csp": c.current_code, "type": d.document_type, "verdict": _verdict(q),
             "how_read": how_read(d), "note": q.correction_notes, "file": d.original_filename} for q, d, c in rows]
