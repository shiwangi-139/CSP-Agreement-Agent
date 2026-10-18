"""app/monitoring.py: the admin Monitoring page and the history it keeps."""
import uuid
from datetime import date, datetime, timedelta, timezone

from app import monitoring
from app.models import (AgreementEvent, CSP, Document, DocumentStatus, LabelSnapshot, ManualReviewQueue, MonitorRun,
                        OutboundMessage, OutboundStatus, ReviewStatus)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _csp(db, phone="9876543210"):
    code = f"9N{uuid.uuid4().int % 1000000:06d}"
    c = CSP(name="Monitor CSP", current_code=code, lookup_code=code, phone=phone, whatsapp_number=phone,
            is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def test_every_tab_answers(db_session):
    db = db_session
    c = _csp(db)
    d = Document(csp_id=c.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, status=DocumentStatus.NEEDS_REVIEW,
                 readability="READABLE", is_current=False, upload_channel="CSP_UPLOAD_PORTAL")
    db.add(d)
    db.flush()
    db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING,
                             reason="The issue date could not be read: enter it", created_at=_now() - timedelta(hours=30)))
    db.commit()
    for name, fn in monitoring.TABS.items():
        out = fn(db)
        assert isinstance(out, dict), name
    r = monitoring.review_queue(db)
    assert r["waiting"] >= 1 and r["status"]["level"] in ("warn", "bad")
    assert ["date not read", 1] in [list(x) for x in r["by_reason"]] or any(k == "date not read" for k, _ in r["by_reason"])


def test_snapshot_and_moves(db_session):
    db = db_session
    c = _csp(db)
    db.commit()
    today = date.today()
    assert monitoring.snapshot_labels(db, today - timedelta(days=3)) >= 1
    db.query(LabelSnapshot).filter(LabelSnapshot.day == today - timedelta(days=3),
                                   LabelSnapshot.csp_id == c.id).update({"category": 4})
    db.commit()
    monitoring.snapshot_labels(db, today)
    db.query(LabelSnapshot).filter(LabelSnapshot.day == today, LabelSnapshot.csp_id == c.id).update({"category": 2})
    db.commit()
    assert monitoring.has_snapshot(db, today)
    moves = monitoring.network(db)["moves_7d"]
    assert moves["up"] >= 1


def test_job_runs_are_kept_and_a_failure_shows(db_session):
    db = db_session
    monitoring.record_run(db, "job:gmail", _now() - timedelta(minutes=5), "FAILED", {"error": "token expired"})
    st = monitoring.agent_status(db)
    gmail = next(j for j in st["jobs"] if j["job"] == "gmail")
    assert gmail["status"]["level"] == "bad" and gmail["last_error"] == "token expired"
    assert st["overall"]["level"] == "bad"
    assert db.query(MonitorRun).filter(MonitorRun.kind == "job:gmail").count() >= 1


def test_csp_messaged_while_their_upload_is_checked_is_flagged(db_session):
    db = db_session
    c = _csp(db, phone="9123456780")
    d = Document(csp_id=c.id, document_type="POLICE_VERIFICATION", sha256=uuid.uuid4().hex,
                 status=DocumentStatus.NEEDS_REVIEW, readability="READABLE", is_current=False)
    db.add(d)
    db.flush()
    db.add(ManualReviewQueue(document_id=d.id, status=ReviewStatus.PENDING, reason="Read as expired on ...",
                             created_at=_now() - timedelta(days=2)))
    for h in (20, 21):                                   # two reminders the same day, while waiting
        db.add(OutboundMessage(csp_id=c.id, template_name="UPLOAD_MISSING", channel="WHATSAPP", destination="x",
                               status=OutboundStatus.SENT, recipient_role="CSP",
                               sent_at=_now() - timedelta(hours=h), created_at=_now() - timedelta(hours=h)))
    db.commit()
    x = monitoring.csp_experience(db)
    assert c.current_code in x["while_in_review"] and c.current_code in x["over_policy"]
    assert x["status"]["level"] == "bad"


def test_wrong_rejection_is_counted(db_session):
    db = db_session
    c = _csp(db)
    d = Document(csp_id=c.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex, status=DocumentStatus.VALID,
                 readability="READABLE", is_current=True)
    db.add(d)
    db.flush()
    db.add(AgreementEvent(csp_id=c.id, event_type="PORTAL_UPLOAD", source="CSP_UPLOAD_PORTAL", channel="WEB",
                          payload={"results": [{"section": "agreement", "ok": False, "reason": "expired",
                                                "rechecked_document_id": d.id}]}))
    db.commit()
    assert monitoring.reading_accuracy(db)["wrong_rejections"]["count"] >= 1
    assert monitoring.csp_experience(db)["wrong_negative_messages"] >= 1
