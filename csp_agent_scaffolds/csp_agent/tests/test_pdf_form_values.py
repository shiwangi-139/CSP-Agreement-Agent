"""A filled-in PDF form (values typed into form boxes, like the agreement of
1A850074 Ashwani Kumar): the values are read where they stand on their line,
so "Valid from 16/10/2025 to 15/10/2027" gives the real end date, not the
1-year default."""
import pymupdf

from app import ocr_service
from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from app.ocr_service import page_text_layer

LINES = ((100, "CUSTOMER SERVICE POINT AGREEMENT"),
         (140, "On this day of, ______________________, CSP Name ______________________ CSP Code ____________ an"),
         (200, 'Eko India Financial Services Pvt. Ltd. ("Eko"), a company incorporated under the Companies Act of India, 1956'),
         (240, "Note: This agreement is Valid from ________________ to ________________."),
         (280, "In consideration of the mutual promises made herein, the Parties agree as follows:"))
BOXES = (((148, 128, 255, 142), "16/10/2025"), ((330, 128, 450, 142), "ashwani kumar"), ((500, 128, 560, 142), "1A850074"),
         ((242, 228, 330, 242), "16/10/2025"), ((345, 228, 435, 242), "15/10/2027"))


def _pdf(boxes=BOXES) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for y, text in LINES:
        page.insert_text((72, y), text, fontsize=10)
    for i, (rect, value) in enumerate(boxes):
        w = pymupdf.Widget()
        w.field_type, w.rect, w.field_name = pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.Rect(*rect), f"f{i}"
        w.field_value, w.text_fontsize = value, 10
        page.add_widget(w)
    return doc.tobytes()


def test_form_values_are_read_in_their_place():
    text = page_text_layer(pymupdf.open(stream=_pdf(), filetype="pdf")[0])
    valid_line = text.split("Valid from", 1)[1].split("\n")[0]
    assert valid_line.split()[:1] == ["16/10/2025"] and "15/10/2027" in valid_line
    assert "CSP Name ashwani kumar CSP Code 1A850074" in text and "____" not in text


def test_the_agreement_range_in_form_boxes_gives_its_end_date(monkeypatch):
    monkeypatch.setattr(ocr_service, "_cache_on", False)
    r = extract_document_fields_deterministic(_pdf(), "agreement.pdf", allow_llm_fallback=False, expected_type="AGREEMENT")
    assert (r["start_date"], r["expiry_date"], r["validity_rule_used"]) == ("2025-10-16", "2027-10-15", "AGREEMENT_RANGE_FROM_TO")


def test_a_pdf_without_form_values_reads_as_before():
    page = pymupdf.open(stream=_pdf(boxes=()), filetype="pdf")[0]
    assert page_text_layer(page) == page.get_text()
