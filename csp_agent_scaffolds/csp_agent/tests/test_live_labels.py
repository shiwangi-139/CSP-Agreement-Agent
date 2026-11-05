"""Live Labels (app/live_labels.py): the saved Label and sub-group change in the
same save as the documents, from any route, with no job in between."""
import sys
import uuid
from datetime import date, timedelta

from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus

TODAY = date.today()


def _csp(s, category=3, sub="3.1"):
    tag = uuid.uuid4().hex[:8].upper()
    c = CSP(name=f"Live {tag}", current_code=f"9L{tag}", category=category, sub_slab=sub,
            is_active_in_calling_sheet=True)
    s.add(c)
    s.flush()
    return c


def _doc(s, c, t, status=DocumentStatus.VALID, expiry=TODAY + timedelta(days=300)):
    d = Document(csp_id=c.id, document_type=t, sha256=uuid.uuid4().hex, readability="READABLE", status=status,
                 is_current=True, issue_date=TODAY - timedelta(days=30), expiry_date=expiry)
    s.add(d)
    return d


def test_label_changes_in_the_same_save_as_the_upload(db_session):
    s = db_session
    c = _csp(s)
    _doc(s, c, "AGREEMENT")
    s.commit()
    assert (c.category, c.sub_slab) == (3, "3.1")        # only the agreement: Label 3
    _doc(s, c, "IIBF_CERTIFICATE", expiry=None)
    s.commit()                                           # e.g. the upload page's save, which never refreshed it
    assert (c.category, c.sub_slab) == (2, "2.2")        # tab and row agree: Label 2 · PVR pending
    assert "Police" in c.category_reason or "PVR" in c.category_reason
    _doc(s, c, "POLICE_VERIFICATION")
    s.commit()
    assert (c.category, c.sub_slab) == (1, "1.1")


def test_a_rejected_document_and_a_review_decision_move_the_label_too(db_session):
    s = db_session
    c = _csp(s, category=1, sub="1.1")
    a = _doc(s, c, "AGREEMENT")
    _doc(s, c, "POLICE_VERIFICATION")
    _doc(s, c, "IIBF_CERTIFICATE", expiry=None)
    s.commit()
    assert c.category == 1
    a.status = DocumentStatus.REJECTED
    s.commit()
    assert (c.category, c.sub_slab) == (2, "2.1")
    # A Review queue decision alone (the document row already saved) refreshes it as well.
    w = _doc(s, c, "AGREEMENT", status=DocumentStatus.NEEDS_REVIEW)
    s.flush()
    q = ManualReviewQueue(document_id=w.id, reason="check", status=ReviewStatus.PENDING)
    s.add(q)
    s.commit()
    c.category, c.sub_slab = 4, "4.1"                     # stale on purpose
    s.commit()
    w.status = DocumentStatus.VALID
    s.commit()
    assert c.category == 1
    c.category, c.sub_slab = 4, "4.1"
    s.commit()
    q.status = ReviewStatus.APPROVED
    s.commit()
    assert (c.category, c.sub_slab) == (1, "1.1")


def test_an_expired_document_shows_as_overdue_and_rollback_changes_nothing(db_session):
    s = db_session
    c = _csp(s, category=4, sub="4.1")
    _doc(s, c, "AGREEMENT", expiry=TODAY - timedelta(days=1))
    _doc(s, c, "POLICE_VERIFICATION")
    _doc(s, c, "IIBF_CERTIFICATE", expiry=None)
    s.commit()
    assert (c.category, c.sub_slab) == (1, "1.3")
    _doc(s, c, "AGREEMENT", status=DocumentStatus.REJECTED)
    s.rollback()
    c.category = 4
    s.commit()                                           # no document change: the CSP row is saved as given
    assert c.category == 4


def _script(monkeypatch, test_engine, *args):
    from sqlalchemy.orm import sessionmaker
    from scripts import refresh_labels
    monkeypatch.setattr(refresh_labels, "SessionLocal", sessionmaker(bind=test_engine))     # the test database
    monkeypatch.setattr(sys, "argv", ["refresh_labels", *args])
    refresh_labels.main()


def test_refresh_labels_script_lists_then_fixes(db_session, test_engine, monkeypatch, capsys):
    s = db_session
    c = _csp(s)
    _doc(s, c, "AGREEMENT")
    _doc(s, c, "IIBF_CERTIFICATE", expiry=None)
    s.commit()
    c.category, c.sub_slab = 3, "3.1"                     # as left by the old upload routes
    s.commit()
    _script(monkeypatch, test_engine, "--csp", c.current_code)
    out = capsys.readouterr().out
    assert "Label 3 -> Label 2: 1" in out and "Dry run" in out
    s.expire_all()
    assert s.get(CSP, c.id).category == 3
    _script(monkeypatch, test_engine, "--csp", c.current_code, "--apply")
    assert "1 of 1 CSP(s) updated" in capsys.readouterr().out
    s.expire_all()
    assert (s.get(CSP, c.id).category, s.get(CSP, c.id).sub_slab) == (2, "2.2")
