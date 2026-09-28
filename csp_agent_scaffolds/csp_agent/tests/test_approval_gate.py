"""
Proves the core safety property from our design discussion: an AI
AUTO_ACCEPT_CANDIDATE decision must never, by itself, renew an agreement or
mark a document VALID. Only an authorized reviewer calling
POST /api/review/{id}/approve-renewal can do that.
"""
import uuid
from datetime import date, timedelta

import pytest

from app.models import (
    CSP, InternalUser, Document, DocumentStatus, ManualReviewQueue,
    ReviewStatus, Agreement, RenewalStatus,
)
from app.api import documents as documents_api
from app.api import review as review_api
from app.ai.schemas import AgreementExtraction, FieldConfidence, VisualCheck


def _unique_code() -> str:
    return f"CSP{uuid.uuid4().int % 100000:05d}"


def _fake_high_confidence_extraction(csp_code: str) -> AgreementExtraction:
    return AgreementExtraction(
        document_type="AGREEMENT",
        csp_code=FieldConfidence(value=csp_code, confidence=0.98),
        csp_name=FieldConfidence(value="Test CSP", confidence=0.98),
        agreement_start_date=FieldConfidence(value="2025-01-01", confidence=0.98),
        agreement_expiry_date=FieldConfidence(value="2026-06-01", confidence=0.98),
        signature=VisualCheck(detected=True, confidence=0.95),
        stamp=VisualCheck(detected=True, confidence=0.95),
        issues=[],
        overall_confidence=0.97,
    )


@pytest.fixture()
def review_db_session(db_session, monkeypatch):
    """documents.py and review.py each hold their own SessionLocal/get_db
    reference -- point those at the same test session used by db_session.
    """
    def _get_test_db():
        yield db_session

    monkeypatch.setattr(documents_api, "SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.api.review.get_db", _get_test_db)
    return db_session


def test_auto_accept_candidate_never_sets_valid_directly(review_db_session, monkeypatch):
    csp = CSP(name="Test CSP", email="test@example.com", lookup_code=_unique_code(), status="ACTIVE")
    review_db_session.add(csp)
    review_db_session.commit()

    doc = Document(csp_id=csp.id, sha256=uuid.uuid4().hex, status=DocumentStatus.PROCESSING,
                    mime_type="application/pdf", storage_path="/tmp/fake.pdf")
    review_db_session.add(doc)
    review_db_session.commit()

    extraction = _fake_high_confidence_extraction(csp.lookup_code)
    monkeypatch.setattr(
        "app.api.documents.ai_service.validate_document",
        lambda *a, **k: extraction,
    )
    monkeypatch.setattr(
        "app.api.documents._extract_pdf_text_pymupdf",
        lambda file_bytes: "irrelevant, extraction is mocked",
    )
    monkeypatch.setattr("app.api.documents.read_file", lambda storage_path: b"%PDF-fake%")

    documents_api._process_document(doc.id, "/tmp/fake.pdf", "application/pdf")

    review_db_session.refresh(doc)
    # The critical assertion: even a 0.97-confidence, zero-issue extraction
    # results in NEEDS_APPROVAL, never VALID.
    assert doc.status == DocumentStatus.NEEDS_APPROVAL
    assert doc.status != DocumentStatus.VALID

    queue_item = review_db_session.query(ManualReviewQueue).filter_by(document_id=doc.id).first()
    assert queue_item is not None
    assert queue_item.reason == "AWAITING_RENEWAL_APPROVAL"
    assert queue_item.status == ReviewStatus.PENDING


def test_approve_renewal_requires_authorized_role(review_db_session):
    csp = CSP(name="Test CSP", lookup_code=_unique_code(), status="ACTIVE")
    review_db_session.add(csp)
    review_db_session.commit()

    unauthorized_user = InternalUser(name="Some RM", email=f"{uuid.uuid4().hex}@example.com", role="RM")
    review_db_session.add(unauthorized_user)
    review_db_session.commit()

    doc = Document(csp_id=csp.id, sha256=uuid.uuid4().hex, status=DocumentStatus.NEEDS_APPROVAL,
                    extracted_fields=_fake_high_confidence_extraction(csp.lookup_code).model_dump(mode="json"))
    review_db_session.add(doc)
    review_db_session.commit()

    queue_item = ManualReviewQueue(document_id=doc.id, reason="AWAITING_RENEWAL_APPROVAL")
    review_db_session.add(queue_item)
    review_db_session.commit()

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        review_api.approve_renewal(
            queue_item.id,
            review_api.RenewalApproval(reviewer_id=unauthorized_user.id),
            db=review_db_session,
        )
    assert exc_info.value.status_code == 403


def test_approve_renewal_by_authorized_dc_renews_agreement(review_db_session):
    csp = CSP(name="Test CSP", lookup_code=_unique_code(), status="ACTIVE")
    review_db_session.add(csp)
    review_db_session.commit()

    dc_user = InternalUser(name="Some DC", email=f"{uuid.uuid4().hex}@example.com", role="DC")
    review_db_session.add(dc_user)
    review_db_session.commit()

    extraction = _fake_high_confidence_extraction(csp.lookup_code)
    doc = Document(csp_id=csp.id, sha256=uuid.uuid4().hex, status=DocumentStatus.NEEDS_APPROVAL,
                    extracted_fields=extraction.model_dump(mode="json"))
    review_db_session.add(doc)
    review_db_session.commit()

    queue_item = ManualReviewQueue(document_id=doc.id, reason="AWAITING_RENEWAL_APPROVAL")
    review_db_session.add(queue_item)
    review_db_session.commit()

    result = review_api.approve_renewal(
        queue_item.id,
        review_api.RenewalApproval(reviewer_id=dc_user.id),
        db=review_db_session,
    )

    assert result["status"] == "RENEWED"

    review_db_session.refresh(doc)
    assert doc.status == DocumentStatus.VALID

    agreement = review_db_session.query(Agreement).filter_by(csp_id=csp.id).first()
    assert agreement is not None
    assert agreement.renewal_status == RenewalStatus.RENEWED
    assert agreement.expiry_date == date(2026, 6, 1)
