"""
app/active_learning.py
The reference set grows by itself (feedback loop): every night (03:30) a few
documents the agent accepted are put in the Review queue as "Reference
check", picked for what a person's answer teaches, not at random:

  + the reading's own risk score (app/safety_gate.py)
  + 30  a rule that read it has fewer than 5 documents in the reading check
  + 25  a rule that read it is on watch
  + up to 20  how it was read (photo, scan, PDF, model) is rare among the
        documents a person has decided

At most REFERENCE_DAILY a night (8; 0 switches it off), spread over the three
document types and at most 2 per rule, and none while 3 nights' worth are
still waiting. Each answer becomes a correct answer for the nightly reading
check (scripts/eval_reading.py). The documents themselves don't change.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .accuracy import REFERENCE, how_read
from .compliance import canonical_type
from .models import Document, DocumentStatus, ManualReviewQueue, ReviewStatus, RuleHealth

LOOK_BACK_DAYS = 60
TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def candidates(db: Session, now: Optional[datetime] = None) -> list[tuple[int, Document, list[str]]]:
    """(usefulness, document, why) for accepted documents nobody checked, best first."""
    now = now or _now()
    in_queue = {i for (i,) in db.query(ManualReviewQueue.document_id)}
    health = {h.rule: h for h in db.query(RuleHealth)}
    decided = Counter()
    for d in db.query(Document).join(ManualReviewQueue, ManualReviewQueue.document_id == Document.id) \
            .filter(ManualReviewQueue.status != ReviewStatus.PENDING):
        decided[how_read(d)] += 1
    total = sum(decided.values()) or 1
    out = []
    for d in db.query(Document).filter(Document.status == DocumentStatus.VALID, Document.readability == "READABLE",
                                       Document.uploaded_at >= now - timedelta(days=LOOK_BACK_DAYS)):
        if d.id in in_queue or canonical_type(d.document_type) not in TYPES:
            continue
        score, why = 0, []
        risk = (d.extracted_fields or {}).get("risk") or {}
        if risk.get("score"):
            score += risk["score"]
            why.append(f"risk {risk['score']}")
        for rule in {d.date_source, d.validity_rule_used} - {None}:
            h = health.get(rule)
            if h is None or (h.eval_checked or 0) < 5:
                score += 30
                why.append(f"few checked answers for '{rule}'")
            if h is not None and h.state == "watch":
                score += 25
                why.append(f"'{rule}' is on watch")
        rare = round(20 * (1 - decided[how_read(d)] / total))
        if rare >= 10:
            score += rare
            why.append(f"few decided {how_read(d)} documents")
        if score:
            out.append((score, d, why))
    out.sort(key=lambda x: (-x[0], -x[1].id))
    return out


def daily_batch(db: Session, n: Optional[int] = None, now: Optional[datetime] = None) -> dict:
    from .config import REFERENCE_DAILY
    n = REFERENCE_DAILY if n is None else n
    if n <= 0:
        return {"added": 0, "note": "switched off (REFERENCE_DAILY=0)"}
    waiting = db.query(ManualReviewQueue).filter(ManualReviewQueue.status == ReviewStatus.PENDING,
                                                 ManualReviewQueue.reason.like(f"{REFERENCE}%")).count()
    if waiting >= 3 * n:
        return {"added": 0, "note": f"{waiting} reference checks still waiting"}
    per_type, per_rule, picked = Counter(), Counter(), []
    cap_type = max(1, -(-n // 3))
    for score, d, why in candidates(db, now):
        t = canonical_type(d.document_type)
        rule = d.validity_rule_used or d.date_source
        if per_type[t] >= cap_type or per_rule[rule] >= 2:
            continue
        per_type[t] += 1
        per_rule[rule] += 1
        picked.append(d.id)
        db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason=(
            f"{REFERENCE}: picked because {', '.join(why)}. Check the dates on the document: Accept if right, "
            "or correct them. The answer teaches the nightly reading check.")[:500]))
        if len(picked) >= n:
            break
    db.commit()
    return {"added": len(picked), "documents": picked}
