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
    # No vision model in tests; a test sets calls["model"] to what it "reads".
    monkeypatch.setattr(gmail_ingest, "read_owner_with_model", lambda data, t: calls.get("model"))
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
    _mail(mailbox, old, csp.current_code, csp.name, {"old.pdf": _pvr_pdf(date.today() - timedelta(days=300), csp.name)})
    _mail(mailbox, new, csp.current_code, csp.name, {"new.pdf": _pvr_pdf(date.today() - timedelta(days=5), csp.name)})
    gmail_ingest.process_message(db_session, None, old)
    gmail_ingest.process_message(db_session, None, new)
    docs = db_session.query(Document).filter_by(csp_id=csp.id).order_by(Document.issue_date).all()
    assert [d.is_current for d in docs] == [False, True]
    assert "/expired/" in docs[0].storage_path and "/expired/" not in docs[1].storage_path
    assert "_PVR_REPLACED_" in docs[0].storage_path and "_PVR_ACTIVE_" in docs[1].storage_path


def _named_csp(db, name):
    c = _csp(db)
    c.name = name
    db.flush()
    return c


def _owner_mail(db_session, mailbox, email_csp, pdf):
    mid = "w" + uuid.uuid4().hex[:8]
    _mail(mailbox, mid, email_csp.current_code, email_csp.name, {"agreement.pdf": pdf})
    assert gmail_ingest.process_message(db_session, None, mid) == "PROCESSED"
    return db_session.query(InboundMessage).filter_by(external_message_id=mid).one()


def test_code_on_the_document_beats_the_email_subject(db_session, fake_gmail):
    # Staff wrote "KO <A>" in the subject but attached CSP B's document.
    mailbox, _ = fake_gmail
    a, b = _csp(db_session), _csp(db_session)
    pdf = _pvr_pdf(date.today() - timedelta(days=5), f"{b.name} CSP Code {b.current_code}")
    inbound = _owner_mail(db_session, mailbox, a, pdf)
    assert db_session.query(Document).filter_by(csp_id=a.id).count() == 0
    doc = db_session.query(Document).filter_by(csp_id=b.id).one()
    assert doc.is_current and doc.status.value == "VALID"
    assert inbound.attachment_decisions[0]["csp_code"] == b.current_code


def test_document_naming_another_csp_is_held_for_review(db_session, fake_gmail):
    # The Rahbar case: no readable code, but the name is another CSP's.
    from app.models import ManualReviewQueue
    mailbox, _ = fake_gmail
    a = _named_csp(db_session, "RAJESH RAJESH")
    b = _named_csp(db_session, "RAHBAR AMAAN SIDDIQUI")
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "RAHBAR AMAAN SIDDIQUI SO SAMIULLAH"))
    doc = db_session.query(Document).filter_by(csp_id=a.id).one()
    assert doc.status.value == "NEEDS_REVIEW" and not doc.is_current
    q = db_session.query(ManualReviewQueue).filter_by(document_id=doc.id).one()
    assert b.current_code in q.reason and "RAHBAR" in q.reason.upper()


def test_document_without_readable_code_or_name_is_held_for_review(db_session, fake_gmail):
    # The handwritten case: nothing on the page says whose it is.
    mailbox, _ = fake_gmail
    a = _named_csp(db_session, "JAY PRAKASH")
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "XXXX"))
    doc = db_session.query(Document).filter_by(csp_id=a.id).one()
    assert doc.status.value == "NEEDS_REVIEW" and not doc.is_current
    assert "no CSP code or name" in doc.extracted_fields["owner_check"]


def test_joined_name_on_document_still_confirms_the_csp(db_session, fake_gmail):
    mailbox, _ = fake_gmail
    a = _named_csp(db_session, "MUKESH KUMAR SINGH")
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "MUKESHKUMARSINGH"))
    doc = db_session.query(Document).filter_by(csp_id=a.id).one()
    assert doc.status.value == "VALID" and doc.is_current


def test_one_letter_spelling_difference_still_confirms_the_csp(db_session, fake_gmail):
    mailbox, _ = fake_gmail
    a = _named_csp(db_session, "PARDEEP SINGH")
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "PRADEEP SINGH"))
    assert db_session.query(Document).filter_by(csp_id=a.id).one().status.value == "VALID"


def test_model_reading_this_csps_handwritten_code_confirms_it(db_session, fake_gmail):
    mailbox, calls = fake_gmail
    a = _named_csp(db_session, "BHANU PRATAP SINGH")
    calls["model"] = {"csp_code": a.current_code, "csp_name": None}
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "XXXX"))
    assert db_session.query(Document).filter_by(csp_id=a.id).one().status.value == "VALID"


def test_model_code_and_name_of_another_csp_moves_the_document(db_session, fake_gmail):
    # The Bhanu Pratap case: filed under Jay Prakash by the email subject.
    mailbox, calls = fake_gmail
    jay = _named_csp(db_session, "JAY PRAKASH")
    bhanu = _named_csp(db_session, "BHANU PRATAP SINGH")
    calls["model"] = {"csp_code": bhanu.current_code, "csp_name": "Bhanu Pratap Singh"}
    _owner_mail(db_session, mailbox, jay, _pvr_pdf(date.today() - timedelta(days=5), "XXXX"))
    assert db_session.query(Document).filter_by(csp_id=jay.id).count() == 0
    assert db_session.query(Document).filter_by(csp_id=bhanu.id).one().is_current


def test_model_code_alone_never_moves_a_document(db_session, fake_gmail):
    # A misread handwritten digit can point at a real CSP: only review.
    mailbox, calls = fake_gmail
    jay = _named_csp(db_session, "JAY PRAKASH")
    other = _named_csp(db_session, "SOMEONE ELSE")
    calls["model"] = {"csp_code": other.current_code, "csp_name": "Bhanu Pratap Singh"}
    _owner_mail(db_session, mailbox, jay, _pvr_pdf(date.today() - timedelta(days=5), "XXXX"))
    doc = db_session.query(Document).filter_by(csp_id=jay.id).one()
    assert doc.status.value == "NEEDS_REVIEW" and other.current_code in doc.extracted_fields["owner_check"]


def test_reviewer_approval_teaches_the_name(db_session, fake_gmail):
    from app.models import ExtractionCorrection
    mailbox, calls = fake_gmail
    a = _named_csp(db_session, "MOHD AYUB KHAN")
    calls["model"] = {"csp_code": None, "csp_name": "Mahmmad Ayub"}
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=5), "XXXX"))
    first = db_session.query(Document).filter_by(csp_id=a.id).one()
    assert first.status.value == "NEEDS_REVIEW" and first.extracted_fields["owner_seen_name"] == "Mahmmad Ayub"
    # What the dashboard's Accept button records:
    db_session.add(ExtractionCorrection(document_id=first.id, field_name="owner_name",
                                        ai_value="Mahmmad Ayub", human_value=a.name))
    db_session.flush()
    calls["model"] = None
    _owner_mail(db_session, mailbox, a, _pvr_pdf(date.today() - timedelta(days=4), "MAHMMAD AYUB"))
    second = db_session.query(Document).filter(Document.csp_id == a.id, Document.id != first.id).one()
    assert second.status.value == "VALID"


def test_reading_an_email_again_adds_only_files_never_stored(db_session, fake_gmail):
    # scripts/reprocess_emails.py: a file already stored (even under another
    # CSP) stays as it is; a file that was rejected before gets a fresh read.
    mailbox, _ = fake_gmail
    a, b = _csp(db_session), _csp(db_session)
    known = _pvr_pdf(date.today() - timedelta(days=30), b.name)
    first = "r" + uuid.uuid4().hex[:8]
    _mail(mailbox, first, b.current_code, b.name, {"known.pdf": known})
    gmail_ingest.process_message(db_session, None, first)
    assert db_session.query(Document).filter_by(csp_id=b.id).count() == 1

    again = "r" + uuid.uuid4().hex[:8]
    fresh = _pvr_pdf(date.today() - timedelta(days=3), a.name)
    _mail(mailbox, again, a.current_code, a.name, {"known.pdf": known, "fresh.pdf": fresh})
    assert gmail_ingest.process_message(db_session, None, again, known_anywhere=True) == "PROCESSED"
    assert db_session.query(Document).filter_by(csp_id=b.id).count() == 1     # untouched, no copy
    assert db_session.query(Document).filter_by(csp_id=a.id).count() == 1     # only the new file
    decisions = db_session.query(InboundMessage).filter_by(external_message_id=again).one().attachment_decisions
    assert [d["decision"] for d in decisions] == ["DUPLICATE", "READABLE"]


def _logo_png(w=72, h=41) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (0, 90, 60)).save(buf, format="PNG")
    return buf.getvalue()


def test_email_signature_logos_are_not_stored(db_session, fake_gmail):
    # image002.jpg / image003.jpg: the Deloitte / Great Place to Work badges.
    mailbox, _ = fake_gmail
    a = _csp(db_session)
    mid = "s" + uuid.uuid4().hex[:8]
    _mail(mailbox, mid, a.current_code, a.name,
          {"image002.png": _logo_png(41, 60), "image003.png": _logo_png(), "pvr.pdf": _pvr_pdf(date.today(), a.name)})
    gmail_ingest.process_message(db_session, None, mid)
    assert db_session.query(Document).filter_by(csp_id=a.id).count() == 1
    decisions = db_session.query(InboundMessage).filter_by(external_message_id=mid).one().attachment_decisions
    assert [d["decision"] for d in decisions] == ["SKIPPED_SIGNATURE_IMAGE", "SKIPPED_SIGNATURE_IMAGE", "READABLE"]


def test_valid_pvr_is_current_before_pcc_and_pcc_before_expired_pvr(db_session):
    from app.document_service import recompute_current
    from app.models import DocumentStatus
    a = _csp(db_session)
    def doc(issue, expiry, rule):
        d = Document(csp_id=a.id, document_type="POLICE_VERIFICATION", sha256=uuid.uuid4().hex, readability="READABLE",
                     status=DocumentStatus.VALID if expiry is None or expiry >= date.today() else DocumentStatus.EXPIRED,
                     issue_date=issue, expiry_date=expiry, validity_rule_used=rule)
        db_session.add(d)
        db_session.flush()
        return d
    old_pvr = doc(date.today() - timedelta(days=500), date.today() - timedelta(days=135), "PVR_DEFAULT_1_YEAR")
    pcc = doc(date.today() - timedelta(days=900), None, "PVR_PCC_LIFETIME")
    assert recompute_current(db_session, a, "POLICE_VERIFICATION") is pcc          # PCC beats an expired PVR
    new_pvr = doc(date.today() - timedelta(days=30), date.today() + timedelta(days=335), "PVR_STATED_1_YEAR")
    assert recompute_current(db_session, a, "POLICE_VERIFICATION") is new_pvr     # valid PVR beats the PCC
    assert not old_pvr.is_current and not pcc.is_current


def test_same_iibf_certificate_sent_again_as_a_new_photo_is_not_stored_twice(db_session):
    # 1A850776: "WhatsApp Image ... .jpeg" and "iibf.jpeg", both the certificate dated 2019-07-25.
    from app.document_service import store_extracted_document
    a = _csp(db_session)
    ex = {"readability": "READABLE", "document_type": "IIBF_CERTIFICATE", "start_date": "2019-07-25",
          "expiry_date": "LIFETIME_NO_EXPIRY", "iibf_registration_number": None, "confidence": 0.95}
    first = store_extracted_document(db_session, a, b"\xff\xd8photo-one", "WhatsApp Image.jpeg", "image/jpeg", ex, "GMAIL_INBOUND")
    again = store_extracted_document(db_session, a, b"\xff\xd8photo-two", "iibf.jpeg", "image/jpeg", ex, "GMAIL_INBOUND")
    assert first.decision == "READABLE" and again.decision == "DUPLICATE" and again.document.id == first.document.id
    assert db_session.query(Document).filter_by(csp_id=a.id).count() == 1
