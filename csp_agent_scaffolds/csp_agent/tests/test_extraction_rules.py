"""Date and validity rules, using wording seen in real CSP documents
(names and numbers replaced). No database needed."""
from datetime import date

from app.ai.extraction.rules import extract_agreement, extract_pvr, extract_iibf, find_dates, add_months

TODAY = date(2026, 9, 25)


# ---------------------------------------------------------------- dates
def test_date_formats():
    text = ("20/04/2024 | 5/1/2024 | 2026.01.28 18:29:17 | 07-05-2025 | 13-Feb-2025 11:29 AM | "
            "08th JUL 2022 | 13\" day of February 2025 | February 3, 2025 | २९/०५/२०२५ | 07 मई 2025")
    got = [f.value for f in find_dates(text)]
    assert got == [date(2024, 4, 20), date(2024, 1, 5), date(2026, 1, 28), date(2025, 5, 7),
                   date(2025, 2, 13), date(2022, 7, 8), date(2025, 2, 13), date(2025, 2, 3),
                   date(2025, 5, 29), date(2025, 5, 7)]


def test_add_months_end_of_month():
    assert add_months(date(2025, 8, 31), 6) == date(2026, 2, 28)
    assert add_months(date(2025, 3, 15), 6) == date(2025, 9, 15)


# ------------------------------------------------------------ agreement
ESTAMP_PAGE = """INDIA NON JUDICIAL  Government of Uttar Pradesh  e-Stamp
Certificate No. : IN-UP95132906450224X
Certificate Issued Date : 13-Feb-2025 11:29 AM
Account Reference : NEWIMPACC (SV)/ up14084604/ NOIDA/ UP-GBN
Stamp Duty Amount(Rs.) : 100 (One Hundred only)
"""
HEADING_PAGE = """CUSTOMER SERVICE POINT AGREEMENT
(This Agreement shall remain valid for a period of three (3) years from the Effective Date.)
On this day of, 29/11/2025 ("Effective Date"), CSP Name RAVI KUMAR CSP Code 1A850546
Version 2.1.10.SBI.Delhi-NCR Page 1 of 13 03/02/2026
"""


def test_estamp_certificate_issue_date_wins_over_heading_date():
    r = extract_agreement(ESTAMP_PAGE + "\n" + HEADING_PAGE, TODAY)
    assert r["issue_date"] == date(2025, 2, 13)
    assert r["date_source"] == "AGREEMENT_ESTAMP_CERT_ISSUE_DATE"
    assert r["validity_years"] == 3 and r["has_explicit_3year_clause"]


def test_estamp_label_mangled_by_ocr():
    text = "e-Stamp Certificato No. > IN-PB85754885471251W Coitigate Issued Date + 04-Dec-2024 03:05 PM Cortifitate Issued By"
    assert extract_agreement(text, TODAY)["issue_date"] == date(2024, 12, 4)


def test_estamp_value_after_certificate_number_in_table_layout():
    text = "Certificate No. Certificate Issued Date IN-UP83434998578628Y ~N \\ 30-Mar-2026 05: 02 PM"
    assert extract_agreement(text, TODAY)["issue_date"] == date(2026, 3, 30)


def test_heading_date_when_no_estamp_page():
    r = extract_agreement(HEADING_PAGE, TODAY)
    assert r["issue_date"] == date(2025, 11, 29)
    assert r["date_source"] == "AGREEMENT_HEADING_DATE"


def test_heading_date_written_as_words():
    text = "CUSTOMER SERVICE POINT AGREEMENT 3 E On this day of, 13\" day of February 2025 (“Effective Date”), CSP Name MR. X"
    assert extract_agreement(text, TODAY)["issue_date"] == date(2025, 2, 13)


def test_three_year_clause_garbled_by_ocr():
    text = "CUSTOMER SERVICE POINT AGREEMENT (Thrs Agreement shal rerna n lalid for a per od oJ three (3) yea.s frorn the Effe.i !e Date.)"
    assert extract_agreement(text, TODAY)["validity_years"] == 3


def test_no_validity_stated_means_one_year():
    text = "CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 16/02/2024 (“Effective Date”), Prince Kumar"
    r = extract_agreement(text, TODAY)
    assert r["issue_date"] == date(2024, 2, 16)
    assert r["validity_years"] == 1 and r["validity_source"] == "AGREEMENT_DEFAULT_1_YEAR"


def test_valid_from_to_range_sets_expiry():
    text = ("CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 01/04/2024 (\"Effective Date\")\n"
            "... page 7 ...\nThis agreement is valid from 01/04/2024 to 31/03/2026 unless terminated.")
    r = extract_agreement(text, TODAY)
    assert r["issue_date"] == date(2024, 4, 1)
    assert r["explicit_expiry"] == date(2026, 3, 31)
    assert r["validity_source"] == "AGREEMENT_RANGE_FROM_TO"


def test_hindi_range():
    r = extract_agreement("यह अनुबंध 01/04/2024 से 31/03/2025 तक मान्य है", TODAY)
    assert r["explicit_expiry"] == date(2025, 3, 31)


def test_future_dates_are_not_issue_dates():
    text = "CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 01/01/2030 (\"Effective Date\")"
    assert extract_agreement(text, TODAY)["issue_date"] is None


# ------------------------------------------------------------------ PVR
UP_PVR = """Application No. - 202503277853 Date - 07-05-2025 CHARACTER CERTIFICATE
This is to certify that Mr. ASHISH KUMAR ... no adverse entry was found against the said
candidate in the police records. This certificate is valid only for one year.
Digitally signed by KESHAV KUMAR
Date: 2025.05.09 18:29:17 +05'30'
"""


def test_pvr_digital_signature_date_beats_top_date():
    r = extract_pvr(UP_PVR, TODAY)
    assert r["issue_date"] == date(2025, 5, 9)
    assert r["date_source"] == "PVR_DIGITAL_SIGNATURE_DATE"
    assert r["validity_months"] == 12


def test_pvr_top_date_when_no_signature():
    r = extract_pvr(UP_PVR.split("Digitally")[0], TODAY)
    assert r["issue_date"] == date(2025, 5, 7)
    assert r["date_source"] == "PVR_TOP_DATE"


def test_pvr_six_months():
    r = extract_pvr("Date- 29/05/2025 It is certified that ... This certificate is valid for six months from the date of issue.", TODAY)
    assert r["validity_months"] == 6
    assert r["validity_source"] == "PVR_STATED_6_MONTHS"


def test_pvr_six_months_hindi():
    r = extract_pvr("दिनांक ०३/०५/२०२५ चरित्र प्रमाण पत्र ... यह प्रमाण पत्र छह माह तक वैध है।", TODAY)
    assert r["issue_date"] == date(2025, 5, 3)
    assert r["validity_months"] == 6


def test_pvr_printed_valid_upto():
    r = extract_pvr("Date: 10/07/2025 ... Valid upto: 09/01/2026", TODAY)
    assert r["explicit_expiry"] == date(2026, 1, 9)
    assert r["validity_source"] == "PVR_PRINTED_EXPIRY"


def test_pvr_default_one_year():
    assert extract_pvr("Approval Date: 10/07/2025", TODAY)["validity_months"] == 12


# ----------------------------------------------------------------- IIBF
IIBF = """INSTITUTE OF BANKING & FINANCE Membership No./ 193037 do hereby certify that DEEPAK SHAKYA
CERTIFICATE EXAMINATION FOR BUSINESS CORRESPONDENTS / FACILITATORS of the Institute.
MUMBAI, DATED 08th JUL 2022 BISWA KETAN DAS
Digitally signed by DS INDIAN INSTITUTE OF BANKING AND FINANCE 3 Date: 2022.07.29 10:47:17 IST
"""


def test_iibf_fields():
    r = extract_iibf(IIBF, TODAY)
    assert r["issue_date"] == date(2022, 7, 8)
    assert r["date_source"] == "IIBF_DATED"
    assert r["holder_name"] == "Deepak Shakya"
    assert r["registration_number"] == "193037"


def test_iibf_signature_date_fallback():
    r = extract_iibf("Digitally signed by DS INDIAN INSTITUTE OF BANKING AND FINANCE 3 Date: 2023.02.14 16:28:59 IST", TODAY)
    assert r["issue_date"] == date(2023, 2, 14)
    assert r["date_source"] == "IIBF_DIGITAL_SIGNATURE_DATE"


def test_ocr_slips_fixed_but_ambiguous_day_not_guessed():
    assert [f.value for f in find_dates("Issued 27-0ct-2025")] == [date(2025, 10, 27)]
    assert [f.value for f in find_dates("2O24-05-12")] == [date(2024, 5, 12)]
    assert find_dates("Certificate Issued Date B7-0ct-2025") == []


def test_three_year_clause_when_ocr_moves_years_to_another_line():
    # Real OCR of a slanted agreement heading (CSP 1A850376, e-stamp 30-04-2026).
    text = ("INDIA NON JUDICIAL e-Stamp\nCertificate No. : IN-UP36891070549881Y\n"
            "Certificate Issued Date : 30-Apr-2026 02:12 PM\n"
            "CUSTOMER SERVICE POINT Ts from the\n"
            "(This Agreement shall remain valid for a period of three (3)\n"
            "Vi csP cose ANGELA Q\nOn this day of, OY | 25 CSP N _ AVADK RAM —\n")
    r = extract_agreement(text, TODAY)
    assert r["issue_date"] == date(2026, 4, 30)
    assert r["validity_years"] == 3 and r["validity_source"] == "AGREEMENT_EXPLICIT_3_YEAR"


def test_three_months_is_not_three_years():
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\nvalid for a period of three (3) months\n"
                          "On this day of, 01/04/2024 (\"Effective Date\")", TODAY)
    assert r["validity_years"] == 1


def test_blank_valid_upto_in_term_clause_means_default_one_year():
    # CSP 1A850455: no 3-year line; term clause "valid upto ____ or until terminated" left blank.
    from app.ai.extraction.deterministic_extractor import calculate_document_expiry
    text = ("Certificate Issued Date : 08-Apr-2026 01:18 PM\nCUSTOMER SERVICE POINT AGREEMENT\n"
            "On this day of, 08/04/2026 (\"Effective Date\"), CSP Name Gajendra Kumar\n"
            "Term: This Agreement shall commence on the Effective Date and shall be valid upto\n"
            "or until terminated in accordance with Clause I(4)B or Clause III(1).")
    r = extract_agreement(text, TODAY)
    assert r["issue_date"] == date(2026, 4, 8) and r["validity_years"] == 1 and r["explicit_expiry"] is None
    exp = calculate_document_expiry("AGREEMENT", r["issue_date"], has_explicit_3year_clause=False, today=TODAY)
    assert exp["calculated_expiry"] == date(2027, 4, 8)
