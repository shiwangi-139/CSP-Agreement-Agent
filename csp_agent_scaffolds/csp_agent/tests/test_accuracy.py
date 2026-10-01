"""app/accuracy.py: spot checks and accuracy from reviewers' answers."""
import uuid
from datetime import date

from app import accuracy
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus


def _doc(db, csp, doc_type="POLICE_VERIFICATION", status=DocumentStatus.VALID, method="RULES_DIGITAL_PYMUPDF"):
    d = Document(csp_id=csp.id, document_type=doc_type, sha256=uuid.uuid4().hex, readability="READABLE",
                 status=status, is_current=True, issue_date=date(2026, 1, 10), extraction_method=method,
                 mime_type="application/pdf")
    db.add(d)
    db.flush()
    return d


def _csp(db):
    code = f"4Q{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Accuracy Csp", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def test_spot_checks_pick_about_the_set_share_and_never_change_status(db_session):
    c = _csp(db_session)
    docs = [_doc(db_session, c) for _ in range(400)]
    picked = sum(accuracy.maybe_spot_check(db_session, d, rate=0.05) for d in docs)
    assert 8 <= picked <= 35                                   # about 5% of 400
    assert all(d.status == DocumentStatus.VALID for d in docs)  # still counted as usual
    held = _doc(db_session, c, status=DocumentStatus.NEEDS_REVIEW)
    assert accuracy.maybe_spot_check(db_session, held, rate=1.0) is False   # only auto-accepted ones


def test_accuracy_comes_from_reviewers_answers(db_session):
    c = _csp(db_session)
    answers = [ReviewStatus.APPROVED] * 7 + [ReviewStatus.CORRECTED] * 2 + [ReviewStatus.REJECTED]
    for a in answers:
        d = _doc(db_session, c, doc_type="AGREEMENT")
        db_session.add(ManualReviewQueue(document_id=d.id, status=a, reason=f"{accuracy.SPOT}: x"))
    db_session.flush()
    s = accuracy.stats(db_session)
    agr = s["by_type"]["AGREEMENT"]
    assert agr["checked"] >= 10 and agr["correct"] >= 7
    assert s["overall"]["accuracy"] is not None and 0 <= s["overall"]["accuracy"] <= 100
    assert any(m["verdict"] == "wrong document or CSP" for m in accuracy.mistakes(db_session))


def test_reference_check_is_added_once(db_session):
    c = _csp(db_session)
    d = _doc(db_session, c)
    assert accuracy.add_reference(db_session, d) is True
    db_session.flush()
    assert accuracy.add_reference(db_session, d) is False
