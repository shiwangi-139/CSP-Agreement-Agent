"""
Shared logic for deciding a campaign_target's status from what's actually
been submitted -- kept out of email_ingest.py so the same completeness
check can be reused wherever a submission changes (email, future web
upload, future WhatsApp).
"""
from sqlalchemy.orm import Session

from .models import RequiredDocument, SubmissionItem, TargetStatus, CampaignTarget

# A submission_item at one of these statuses counts as "satisfied" for
# completeness purposes -- REJECTED and PENDING do not.
_SATISFIED_ITEM_STATUSES = {"RECEIVED", "NEEDS_APPROVAL", "VALID"}


def evaluate_target_completeness(db: Session, target: CampaignTarget) -> TargetStatus:
    """Compares what this campaign requires against what's actually been
    submitted for this target, and returns the correct next status. This
    is what distinguishes 'sent the agreement but not the police
    verification' from 'fully submitted, awaiting approval'.
    """
    required_types = {
        r.document_type for r in
        db.query(RequiredDocument).filter(RequiredDocument.campaign_id == target.campaign_id).all()
    }
    if not required_types:
        # Campaign has no explicit requirements configured -- fall back to
        # whatever the single most recent submission_item says.
        required_types = set()

    items = db.query(SubmissionItem).filter(SubmissionItem.campaign_target_id == target.id).all()
    by_type = {item.document_type: item for item in items}

    if any(item.status == "REJECTED" for item in items):
        return TargetStatus.REJECTED_NEEDS_RESUBMISSION

    missing = required_types - set(by_type.keys())
    if missing:
        return TargetStatus.AWAITING_ADDITIONAL_DOCUMENTS

    if any(item.status not in _SATISFIED_ITEM_STATUSES for item in items):
        return TargetStatus.DOCUMENT_NEEDS_REVIEW

    return TargetStatus.DOCUMENT_NEEDS_APPROVAL
