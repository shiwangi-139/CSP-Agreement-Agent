"""The focused re-read (app/ocr_service.py) and how the extractor uses it."""
from datetime import date

import cv2
import numpy as np
import pytest

import app.ai.extraction.deterministic_extractor as ex
from app import ocr_service


def _page_with_stamp() -> bytes:
    """A photo-like page: printed lines, a purple ring stamp over the validity line."""
    img = np.full((1600, 1200, 3), 245, np.uint8)
    lines = ["CUSTOMER SERVICE POINT AGREEMENT", "On this day of 12/07/2025 (Effective Date)",
             "CSP Name ANURADHA  CSP Code 1A852417", "Eko India Financial Services Pvt. Ltd.",
             "This agreement is Valid from 12/07/2025 to 11/07/2028",
             "In consideration of the mutual promises made herein"]
    for i, t in enumerate(lines):
        cv2.putText(img, t, (40, 200 + 120 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (20, 20, 20), 2)
    cv2.circle(img, (300, 680), 110, (160, 40, 120), 9)          # purple seal over the line
    cv2.putText(img, "NOTARY", (220, 690), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (160, 40, 120), 3)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()


def test_stamp_ink_is_whitened_black_text_kept():
    img = np.full((10, 20, 3), 255, np.uint8)
    img[:, :10] = (160, 40, 120)                                   # purple (BGR)
    img[:, 10:] = (20, 20, 20)                                     # black text
    g = ocr_service._stamp_free_gray(img)
    assert g[:, :10].min() == 255 and g[:, 10:].max() < 50


def test_focused_reread_reads_the_validity_line(monkeypatch):
    monkeypatch.setattr(ocr_service, "_cache_on", False)
    scan = ocr_service.scan_document(_page_with_stamp())
    text = ocr_service.focused_reread(scan, "AGREEMENT")
    assert "12/07/2025 to 11/07/2028" in text          # the line the stamp crosses


def test_second_look_settles_a_default_validity(monkeypatch):
    monkeypatch.setattr(ex, "focused_reread", lambda scan, t: "is Valid from\n12/07/2025\nto 11/07/2028")
    scan = ocr_service.DocumentScan("image/jpeg", [ocr_service.PageScan(0, "x", "OCR", 50)], 1)
    first = {"issue_date": date(2025, 7, 12), "validity_source": "AGREEMENT_DEFAULT_1_YEAR", "explicit_expiry": None}
    text = 'CUSTOMER SERVICE POINT AGREEMENT\nOn this day of 12/07/2025 ("Effective Date")'
    r, combined, note = ex._second_look(scan, "AGREEMENT", text, first, date(2026, 10, 18))
    assert r["explicit_expiry"] == date(2028, 7, 11) and "focused re-read" in combined and note is None


def test_second_look_is_not_used_when_the_reading_is_already_sure(monkeypatch):
    monkeypatch.setattr(ex, "focused_reread", lambda scan, t: pytest.fail("must not re-read"))
    scan = ocr_service.DocumentScan("image/jpeg", [ocr_service.PageScan(0, "x", "OCR", 50)], 1)
    sure = {"issue_date": date(2025, 7, 12), "explicit_expiry": date(2028, 7, 11), "validity_source": "AGREEMENT_RANGE_FROM_TO"}
    assert ex._second_look(scan, "AGREEMENT", "t", sure, date(2026, 10, 18))[0] is sure


@pytest.mark.parametrize("typed, review", [(date(2026, 4, 15), False), (None, True), (date(2026, 4, 16), True)])
def test_a_repaired_year_counts_only_when_the_csp_typed_the_same_date(typed, review, monkeypatch):
    import pymupdf
    monkeypatch.setattr(ex, "focused_reread", lambda scan, t: "")
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(["CUSTOMER SERVICE POINT AGREEMENT", "On this day of, 15-04-9026 CSP Name MUKESH SAINI",
                              "This Agreement shall remain valid for a period of three (3) years from the Effective Date.",
                              "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign."]):
        page.insert_text((30, 60 + 20 * i), line, fontsize=8)
    r = ex.extract_document_fields_deterministic(d.tobytes(), "agreement.pdf", allow_llm_fallback=False,
                                                 expected_type="AGREEMENT", typed_issue_date=typed,
                                                 today=date(2026, 10, 18))
    assert r["start_date"] == "2026-04-15" and r["expiry_date"] == "2029-04-15"
    assert bool(r.get("type_review") and "9026" in r["type_review"]) == review
