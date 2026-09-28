"""Speed changes must not change results: Hindi only when needed, duplicates
skip OCR, parallel reading matches one-at-a-time, the OCR cache is used."""
import io
import uuid
from datetime import date, timedelta

from app import gmail_ingest, ocr_service
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.extract_pool import extract_all
from tests.test_gmail_ingest import _csp, _mail, fake_gmail  # noqa: F401  (fixture)
from tests.test_portal_and_api import _pvr_pdf

ENGLISH = " ".join(["This agreement is made between Eko India Financial Services and the partner"] * 4)


def _fake_ocr(monkeypatch, answers):
    calls = []

    def one(gray, lang):
        calls.append(lang)
        return answers[lang]
    monkeypatch.setattr(ocr_service, "_ocr_one", one)
    monkeypatch.setattr(ocr_service, "_languages", lambda: ["eng", "hin", "osd"])
    return calls


def test_hindi_pass_is_skipped_on_a_clean_english_page(monkeypatch):
    calls = _fake_ocr(monkeypatch, {"eng": (ENGLISH, 91.0, 0.02), "hin": ("x", 20.0, 0.9)})
    text, conf = ocr_service._ocr_image(None)
    assert calls == ["eng"] and text == ENGLISH and conf == 91.0


def test_hindi_pass_runs_when_english_reads_badly(monkeypatch):
    calls = _fake_ocr(monkeypatch, {"eng": ("aa ,, ~~ ii", 48.0, 0.6),
                                    "hin": ("चरित्र प्रमाण पत्र निर्गत तिथि 05-03-2026", 78.0, 0.05)})
    text, _ = ocr_service._ocr_image(None)
    assert calls == ["eng", "hin"] and "निर्गत तिथि" in text


def test_parallel_reading_matches_one_at_a_time():
    items = [(_pvr_pdf(date.today() - timedelta(days=d)), f"p{d}.pdf") for d in (5, 40, 200)]
    seq = [extract_document_fields_deterministic(b, n) for b, n in items]
    par = extract_all(items)
    keys = ("readability", "document_type", "start_date", "expiry_date", "date_source", "validity_rule_used")
    assert [[r[k] for k in keys] for r in seq] == [[r[k] for k in keys] for r in par]


def test_ocr_cache_means_a_file_is_ocrd_once(monkeypatch):
    from PIL import Image, ImageDraw
    img = Image.new("L", (1800, 1000), 255)
    ImageDraw.Draw(img).text((50, 50), "CHARACTER CERTIFICATE " + uuid.uuid4().hex, fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    calls = _fake_ocr(monkeypatch, {"eng": (ENGLISH, 91.0, 0.02), "hin": ("", 0.0, 1.0)})
    first = ocr_service.scan_document(data)
    second = ocr_service.scan_document(data)
    assert calls == ["eng"] and first.pages[0].text == second.pages[0].text


def test_duplicate_attachment_never_reaches_ocr(db_session, fake_gmail, monkeypatch):
    mailbox, _ = fake_gmail
    csp = _csp(db_session)
    pdf = _pvr_pdf(date.today() - timedelta(days=9))
    first, again = "a" + uuid.uuid4().hex[:8], "b" + uuid.uuid4().hex[:8]
    _mail(mailbox, first, csp.current_code, csp.name, {"pvr.pdf": pdf})
    _mail(mailbox, again, csp.current_code, csp.name, {"pvr_again.pdf": pdf})
    assert gmail_ingest.process_message(db_session, None, first) == "PROCESSED"

    def no_ocr(*a, **k):
        raise AssertionError("duplicate was sent to OCR")
    monkeypatch.setattr(gmail_ingest, "extract_all", lambda items, **k: [no_ocr() for _ in items])
    monkeypatch.setattr(gmail_ingest, "extract_document_fields_deterministic", no_ocr)
    assert gmail_ingest.process_message(db_session, None, again) == "PROCESSED"
    from app.models import InboundMessage
    dec = db_session.query(InboundMessage).filter_by(external_message_id=again).one().attachment_decisions
    assert dec[0]["decision"] == "DUPLICATE" and dec[0]["document_type"] == "POLICE_VERIFICATION"
