"""The second Gmail inbox (GMAIL_SECOND_TOKEN): read only when switched on,
read-only access, its own saved position, its own message ids, and a
problem there never stops the main inbox."""
import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from app import config, gmail_ingest
from app.comms import gmail_oauth
from app.models import InboundMessage


@pytest.fixture()
def scans(db_session, monkeypatch):
    """_run_scan with fake Gmail services; records which inboxes were scanned."""
    calls = []
    monkeypatch.setattr(gmail_ingest, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(gmail_oauth, "get_gmail_service",
                        lambda token=None, scopes=None: ("service", token.name if token else "main", tuple(scopes or ())))

    def fake_scan(db, service, key, days, mailbox, max_messages):
        calls.append({"service": service, "key": key, "days": days, "mailbox": mailbox})
        if mailbox and calls[0].get("fail_second"):
            raise RuntimeError("second inbox token expired")
        return {"mode": "INCREMENTAL"}
    monkeypatch.setattr(gmail_ingest, "_scan_inbox", fake_scan)
    return calls


def test_second_inbox_is_off_unless_its_token_is_set(scans, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_SECOND_TOKEN", "")
    summary = gmail_ingest._run_scan()
    assert [c["key"] for c in scans] == ["gmail"] and gmail_ingest.SECOND not in summary


def test_second_inbox_is_read_only_with_its_own_position(scans, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_SECOND_TOKEN", "gmail_token_2.json")
    summary = gmail_ingest._run_scan()
    main, second = scans
    assert main["key"] == "gmail" and main["mailbox"] is None and main["days"] == config.GMAIL_BACKFILL_DAYS
    assert second["key"] == "gmail_2" and second["mailbox"] == "second" and second["days"] == config.GMAIL_SECOND_BACKFILL_DAYS
    assert second["service"] == ("service", "gmail_token_2.json", ("https://www.googleapis.com/auth/gmail.readonly",))
    assert summary["second"] == {"mode": "INCREMENTAL"}


def test_a_problem_with_the_second_inbox_never_stops_the_main_one(scans, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_SECOND_TOKEN", "gmail_token_2.json")
    scans.append({"fail_second": True})              # read by fake_scan through calls[0]
    summary = gmail_ingest._run_scan()
    assert summary["mode"] == "INCREMENTAL" and "token expired" in summary["second"]["error"]


def test_the_same_message_id_in_two_inboxes_is_two_emails(db_session, monkeypatch):
    db = db_session
    mid = uuid.uuid4().hex[:16]
    monkeypatch.setattr(gmail_oauth, "get_message", lambda service, m: {
        "subject": "hello", "sender": "Someone <nobody@example.org>", "body": "", "attachments": [],
        "date": "Wed, 28 Oct 2026 10:00:00 +0530", "thread_id": "t"})
    assert gmail_ingest.process_message(db, None, mid) == "IGNORED_NOT_RELEVANT"
    assert gmail_ingest.process_message(db, None, mid, mailbox="second") == "IGNORED_NOT_RELEVANT"
    assert gmail_ingest.process_message(db, None, mid, mailbox="second") == "ALREADY_PROCESSED"
    rows = {r.external_message_id: r.mailbox for r in db.query(InboundMessage)
            .filter(InboundMessage.external_message_id.in_([mid, f"m2:{mid}"]))}
    assert rows == {mid: None, f"m2:{mid}": "second"}
    db.rollback()


def test_status_shows_the_second_inbox(db_session, monkeypatch):
    monkeypatch.setattr(config, "GMAIL_SECOND_TOKEN", "")
    assert gmail_ingest.ingestion_status(db_session)["second"]["on"] is False
    monkeypatch.setattr(config, "GMAIL_SECOND_TOKEN", "gmail_token_2.json")
    s = gmail_ingest.ingestion_status(db_session)["second"]
    assert s["on"] is True and s["backfill_days"] == config.GMAIL_SECOND_BACKFILL_DAYS
