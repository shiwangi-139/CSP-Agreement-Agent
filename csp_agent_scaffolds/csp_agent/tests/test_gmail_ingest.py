"""Read-once Gmail processing with a fake Gmail service (no network)."""
import uuid
from datetime import date, timedelta

import pytest

from app import gmail_ingest
from app.comms import gmail_oauth
from app.models import CSP, Document, InboundMessage
from tests.test_portal_and_api import _pvr_pdf


@pytest.fixture()
def fake_gmail(monkeypatch, tmp_path):
    mailbox, calls = {}, {"attachments": 0}

    def get_message(service, msg_id):
        return mailbox[msg_id]

    def fetch(service, msg_id, att_id):
        calls["attachments"] += 1
        return mailbox[msg_id]["_files"][att_id]

    monkeypatch.setattr(gmail_oauth, "get_message", get_message)
    monkeypatch.setattr(gmail_oauth, "fetch_attachment_bytes", fetch)
    return mailbox, calls


def _mail(mailbox, msg_id, code, name, files, sender="Ganesh Kumar <ganesh.kumar@eko.co.in>"):
    mailbox[msg_id] = {
        "id": msg_id, "thread_id": "t" + msg_id, "label_ids": ["INBOX"], "message_id": f"<{msg_id}@mail>",
        "sender": sender, "subject": f'Request to terminal extension for KO "{code}"',
        "date": "Tue, 26 May 2026 13:43:00 +0530", "internal_date_ms": 0,
        "body": f"Dear Sir,\n\nKindly extend the terminal for Below address.\n\nCSP Code- {code}\nCSP Name- {name}\n",
        "attachments": [{"filename": fn, "mime_type": "application/pdf", "attachment_id": fn, "size": len(b)}
                        for fn, b in files.items()],
        "_files": files,
    }


def _csp(db):
    tag = uuid.uuid4().hex[:6]
    code = f"7X{int(tag, 16) % 1000000:06d}"
    c = CSP(name=f"KAUSHAR {tag}", current_code=code, lookup_code=code, phone="9876511111",
            is_active_in_calling_sheet=True)
    db.add(c)
    db.flush()
    return c


def test_read_once_and_match_by_body_code(db_session, fake_gmail):
    mailbox, calls = fake_gmail
    csp = _csp(db_session)
    pvr = _pvr_pdf(date.today() - timedelta(days=40))
    mid = "m" + uuid.uuid4().hex[:8]
    _mail(mailbox, mid, csp.current_code, csp.name, {"pvr.pdf": pvr})

    assert gmail_ingest.process_message(db_session, None, mid) == "PROCESSED"
    inbound = db_session.query(InboundMessage).filter_by(external_message_id=mid).one()
    assert inbound.csp_id == csp.id and "BODY_CSP_CODE" in inbound.error_message
    assert inbound.attachment_decisions[0]["decision"] == "READABLE"
    assert inbound.sender_on_sheet is True     # @eko.co.in

    before = calls["attachments"]
    assert gmail_ingest.process_message(db_session, None, mid) == "ALREADY_PROCESSED"
    assert calls["attachments"] == before       # nothing downloaded or OCR'd again


def test_same_attachment_in_another_email_is_duplicate(db_session, fake_gmail):
    mailbox, _ = fake_gmail
    csp = _csp(db_session)
    pvr = _pvr_pdf(date.today() - timedelta(days=10))
    a, b = "a" + uuid.uuid4().hex[:8], "b" + uuid.uuid4().hex[:8]
    _mail(mailbox, a, csp.current_code, csp.name, {"pvr.pdf": pvr})
    _mail(mailbox, b, csp.current_code, csp.name, {"pvr_again.pdf": pvr})
    gmail_ingest.process_message(db_session, None, a)
    gmail_ingest.process_message(db_session, None, b)
    assert db_session.query(Document).filter_by(csp_id=csp.id).count() == 1
    second = db_session.query(InboundMessage).filter_by(external_message_id=b).one()
    assert second.attachment_decisions[0]["decision"] == "DUPLICATE"


def test_unknown_csp_is_not_invented(db_session, fake_gmail):
    mailbox, _ = fake_gmail
    mid = "u" + uuid.uuid4().hex[:8]
    _mail(mailbox, mid, "1A000000", "Nobody", {"x.pdf": _pvr_pdf(date.today())}, sender="someone <x@gmail.com>")
    n = db_session.query(CSP).count()
    assert gmail_ingest.process_message(db_session, None, mid) == "UNMATCHED_NO_CSP"
    assert db_session.query(CSP).count() == n
    inbound = db_session.query(InboundMessage).filter_by(external_message_id=mid).one()
    assert inbound.sender_on_sheet is False


def test_newer_document_moves_older_to_expired_folder(db_session, fake_gmail):
    mailbox, _ = fake_gmail
    csp = _csp(db_session)
    old, new = "o" + uuid.uuid4().hex[:8], "n" + uuid.uuid4().hex[:8]
    _mail(mailbox, old, csp.current_code, csp.name, {"old.pdf": _pvr_pdf(date.today() - timedelta(days=300))})
    _mail(mailbox, new, csp.current_code, csp.name, {"new.pdf": _pvr_pdf(date.today() - timedelta(days=5))})
    gmail_ingest.process_message(db_session, None, old)
    gmail_ingest.process_message(db_session, None, new)
    docs = db_session.query(Document).filter_by(csp_id=csp.id).order_by(Document.issue_date).all()
    assert [d.is_current for d in docs] == [False, True]
    assert "/expired/" in docs[0].storage_path and "/expired/" not in docs[1].storage_path
    assert "_PVR_REPLACED_" in docs[0].storage_path and "_PVR_ACTIVE_" in docs[1].storage_path
