"""
Retire every reminder that is still waiting unsent, and let the engine
write ONE fresh draft per open follow-up (today's wording, a live upload
link, no escalations the CSP hasn't earned). For drafts that piled up
while nothing was being sent.

    python -m scripts.redraft_fresh            # dry run: everything is rolled back, numbers only
    python -m scripts.redraft_fresh --apply

Sent messages, messages being sent (approved), manual messages and link
requests are never touched. Retired rows stay in the history as
"Superseded: redrafted fresh".
"""
import argparse
from collections import Counter
from datetime import date

from app import renewal_engine
from app.db import SessionLocal
from app.models import CSP, OutboundMessage, OutboundStatus

RETIRE = (OutboundStatus.DRAFT, OutboundStatus.QUEUED_FOR_REVIEW, OutboundStatus.READY_NOT_SENT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        old = (db.query(OutboundMessage)
               .filter(OutboundMessage.status.in_(RETIRE), OutboundMessage.cycle_id.isnot(None)).all())
        before = Counter(f"{m.channel} {m.recipient_role} {m.template_name}" for m in old)
        for m in old:
            m.status, m.reviewed_by = OutboundStatus.REJECTED, "agent"
            m.reviewed_at = renewal_engine._now()
            m.error_log = f"Superseded: redrafted fresh on {date.today():%d-%m-%Y}"
        db.flush()
        start = db.query(OutboundMessage.id).order_by(OutboundMessage.id.desc()).limit(1).scalar() or 0
        ids = [i for (i,) in db.query(CSP.id).filter(CSP.is_active_in_calling_sheet.is_(True)).order_by(CSP.id)]
        for csp_id in ids:
            renewal_engine.run_for_csp(db, db.get(CSP, csp_id))
            db.flush()
        new = db.query(OutboundMessage).filter(OutboundMessage.id > start).all()
        after = Counter(f"{m.channel} {m.recipient_role} {m.template_name}" for m in new)
        print(f"retire {len(old)} unsent drafts:")
        for k, n in sorted(before.items()):
            print(f"  {n:5}  {k}")
        print(f"write {len(new)} fresh drafts (one per open follow-up):")
        for k, n in sorted(after.items()):
            print(f"  {n:5}  {k}")
        per_csp = Counter(m.csp_id for m in new if m.channel == "WHATSAPP" and m.recipient_role == "CSP")
        print(f"most WhatsApp drafts for one CSP: {max(per_csp.values()) if per_csp else 0}")
        if a.apply:
            db.commit()
            print("applied.")
        else:
            db.rollback()
            print("dry run: nothing changed. Run with --apply to do it.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
