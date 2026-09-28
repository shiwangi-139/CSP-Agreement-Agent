import logging
from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..email_ingest import run_email_ingestion, run_2year_historical_backfill
from ..db import get_db
from ..models import InboundMessage

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/email/run-now")
def trigger_email_ingest():
    """Trigger an immediate Gmail OAuth check for new CSP agreement emails within the 2-year rolling window."""
    try:
        summary = run_email_ingestion()
        return {
            "status": "success",
            "scanned": summary.get("scanned", 0),
            "processed": summary.get("processed", 0),
            "skipped_duplicate": summary.get("skipped_duplicate", 0),
            "skipped_non_csp": summary.get("skipped_non_csp", 0),
            "replies_detected": summary.get("replies_detected", 0),
            "new_agreements": summary.get("new_agreements", 0),
            "summary": summary
        }
    except Exception as e:
        logger.error(f"Error during manual email ingestion: {e}", exc_info=True)
        return {"status": "error", "message": str(e), "scanned": 0, "processed": 0}


@router.post("/email/backfill-2years")
def trigger_2year_historical_backfill(max_total: int = 1000):
    """
    Phase 1: Triggers a comprehensive 2-year (730 days) historical backfill scan of Gmail.
    Scans Inbox, Trash, and Spam, applies deterministic category classification,
    and isolates to Calling Sheet New master data.
    """
    try:
        summary = run_2year_historical_backfill(max_total=max_total)
        return {
            "status": "success",
            "message": f"Historical 2-year backfill completed for up to {max_total} candidate emails.",
            "summary": summary
        }
    except Exception as e:
        logger.error(f"Error during 2-year backfill: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.get("/email/status")
def get_ingestion_status(db: Session = Depends(get_db)):
    """Returns real-time ingestion status and breakdown of ingested emails by category."""
    total_inbound = db.query(InboundMessage).count()
    processed_count = db.query(InboundMessage).filter(InboundMessage.status == "PROCESSED").count()
    ignored_non_csp = db.query(InboundMessage).filter(InboundMessage.status == "IGNORED_NON_CSP").count()
    needs_review = db.query(InboundMessage).filter(InboundMessage.status == "NEEDS_REVIEW").count()

    category_counts = db.query(
        InboundMessage.email_category, func.count(InboundMessage.id)
    ).group_by(InboundMessage.email_category).all()

    cat_breakdown = {
        (cat.value if hasattr(cat, 'value') else str(cat)): cnt
        for cat, cnt in category_counts
    }

    latest_msg = db.query(InboundMessage).order_by(InboundMessage.received_at.desc()).first()

    return {
        "total_emails_ingested": total_inbound,
        "processed": processed_count,
        "ignored_non_csp": ignored_non_csp,
        "needs_review": needs_review,
        "categories": cat_breakdown,
        "last_ingested_at": latest_msg.received_at.isoformat() if latest_msg and latest_msg.received_at else None
    }
