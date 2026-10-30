"""Date and validity rules, using wording seen in real CSP documents
(names and numbers replaced). No database needed."""
from datetime import date

import pytest

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


def test_printed_end_and_close_date_sets_expiry():
    # Scanned Maharashtra agreement: the start date lost a separator and the
    # end date is on the next line.
    text = ("On this day of, 01-07-2025 (\"Effective Date\"), MOHAMMAD MOHSIN\n"
            "S.B.1. C.S.P. KIOSK ID 14851804, VALIDATION FOR YEAR's:- START DATE ON 01 07-2025 (७ —\n"
            "TO END AND CLOSE DATE 01-06-2028 Full 3 year agreement.\n")
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\n" + text, TODAY)
    assert r["issue_date"] == date(2025, 7, 1)
    assert r["explicit_expiry"] == date(2028, 6, 1)


def test_full_3_year_agreement_wording():
    plain = "CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 29/11/2025 (\"Effective Date\")\n"
    assert extract_agreement(plain, TODAY)["validity_years"] == 1
    r = extract_agreement(plain + "This is a Full 3 year agreement.", TODAY)
    assert r["validity_years"] == 3


def test_end_date_too_far_or_before_issue_is_ignored():
    plain = "CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 29/11/2025 (\"Effective Date\")\n"
    for end in ("01-01-2040", "01-01-2024"):
        assert extract_agreement(plain + f"End Date: {end}", TODAY)["explicit_expiry"] is None


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


def test_range_in_the_effective_date_line():
    # 1A852473 Ashish Kumar: "On this day of, 26/03/2025 to 25/03/2027 ("Effective Date")".
    r = extract_agreement('CUSTOMER SERVICE POINT AGREEMENT\nASHISH KUMAR SO RAM JEEWAN On this day of, '
                          '26/03/2025 to 25/03/2027 ("Effective Date"), ASHISH KUMAR an Indian national', TODAY)
    assert r["issue_date"] == date(2025, 3, 26) and r["validity_source"] == "AGREEMENT_RANGE_FROM_TO"
    assert r["explicit_expiry"] == date(2027, 3, 25)


def test_valid_for_two_years_ie_range_in_the_body():
    # 1A850476: no 3-year line; the body says "valid for two years i.e 06-06-2024 to 05-06-2026".
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\nOn this day of 06-06-2024 (Effective Date)\n"
                          "hereby agrees to be a CSP of Eko at the said CSP Location. This agreement would be valid "
                          "for two years i.e 06-06-2024 to 05-06-2026. Eko and CSP have recorded", TODAY)
    assert r["validity_source"] == "AGREEMENT_RANGE_FROM_TO" and r["explicit_expiry"] == date(2026, 6, 5)


def test_dash_range_with_two_digit_start_year():
    r = extract_agreement("Certificate Issued Date 20-Dec-2023\nCUSTOMER SERVICE POINT AGREEMENT "
                          "On this day of 20/12/23 - 19/12/2025 I. RELATIONSHIP DEFINITION", TODAY)
    assert r["explicit_expiry"] == date(2025, 12, 19)


def test_unrelated_dashes_are_not_a_range():
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\nOn this day of 12/03/2024 (Effective Date) "
                          "CSP shall work 9-6 daily; see clauses 3-4; Rs. 5,000-10,000.", TODAY)
    assert r["validity_source"] == "AGREEMENT_DEFAULT_1_YEAR"


# ------------------------------------------------- state certificates (Oct 2026)
JHARKHAND_OCR = """341102606115
निर्गत की तिथि 3-09-2026
13-09-2026
प्रमाणित किया जाता है कि JABBAR ANSARI, जिला - गिरिडीह के चरित्र और पूर्ववृत्त का सत्यापन वर्ष 1996 से 3-09-2026 तक
उपरोक्त पुलिस थाने के अभिलेखों में उनके विरुद्ध कोई प्रतिकूल बात नहीं पाई गई।
पुलिस अधीक्षक गिरिडीह
1. यह एक सिस्टम जनित प्रमाणपत्र है। यह प्रमाणपत्र जारी होने की तिथि से केवल छह महीने के लिए
"""


def test_jharkhand_character_certificate_by_its_body_wording():
    from app.ocr_service import classify_allowlist_gate
    ok, doc_type, reason, _ = classify_allowlist_gate(JHARKHAND_OCR, "", "POLICE_VERIFICATION")
    assert ok and doc_type == "POLICE_VERIFICATION" and reason.startswith("Matched")   # not "Uploaded in ..."


def test_six_months_from_date_of_issue_even_when_valid_word_is_cut_off():
    assert extract_pvr(JHARKHAND_OCR, TODAY)["validity_months"] == 6


def test_dropped_first_digit_of_day_is_restored_from_the_other_reading():
    r = extract_pvr(JHARKHAND_OCR, TODAY)
    assert r["issue_date"] == date(2026, 9, 13)


def test_up_character_certificate_wording():
    from app.ocr_service import classify_allowlist_gate
    text = ("upon verification conducted through the concerned Police Station, Local Intelligence Unit, and "
            "District Crime Record Bureau Commissionerate / District- Gorakhpur, nothing found")
    assert classify_allowlist_gate(text)[1] == "POLICE_VERIFICATION"


def test_bihar_rtps_application_receipt_is_not_a_certificate():
    from app.ocr_service import classify_allowlist_gate
    text = ("लोक सेवाओं का अधिकार बिहार सरकार (आवेदन का विवरण) सेवा का नाम : आचरण प्रमाण-पत्र का निर्गमन "
            "सेवा प्रदान करने की प्रस्तावित तिथि : 03/11/2026")
    assert classify_allowlist_gate(text, "", "POLICE_VERIFICATION")[1] == "PVR_APPLICATION_ONLY"


# ------------------------------------------- agreement validity wordings (real agreements)
PLAIN = "CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 26/10/2024 (\"Effective Date\"), CSP Name ANUP\n"


@pytest.mark.parametrize("clause, expiry", [
    ("Term: This Agreement shall commence on the Effective Date and shall be valid upto 25/10/2027\nor until "
     "terminated in accordance with Clause I(4)B", date(2027, 10, 25)),                  # Anup, Haryana
    ("shall be valid up to: 25-10-2027 or until terminated", date(2027, 10, 25)),
    ("This agreement is valid till 31/03/2027.", date(2027, 3, 31)),
    ("Expiry Date : 25.10.2027", date(2027, 10, 25)),
    ("यह अनुबंध 25/10/2027 तक मान्य है।", date(2027, 10, 25)),
    ("This Agreement shall remain valid for a period of two (2) years from the Effective Date.", date(2026, 10, 26)),
])
def test_agreement_printed_last_day_or_stated_years(clause, expiry):
    from app.expiry_engine import calculate_document_expiry
    r = extract_agreement(PLAIN + clause, TODAY)
    exp = calculate_document_expiry("AGREEMENT", r["issue_date"], explicit_expiry_date=r["explicit_expiry"],
                                    has_explicit_3year_clause=r["has_explicit_3year_clause"], today=TODAY)
    assert r["issue_date"] == date(2024, 10, 26) and exp["calculated_expiry"] == expiry


def test_unrelated_years_and_far_dates_are_not_the_agreement_term():
    text = PLAIN + ("CSP shall keep records for a period of five years. "
                    "valid upto ____ or until terminated. Penalty notice dated 01/01/2040.")
    r = extract_agreement(text, TODAY)
    assert r["explicit_expiry"] is None and r["validity_years"] == 1


def test_issue_date_found_from_on_this_day_of_when_heading_is_garbled():
    text = (",USTOMER SERVICEINT AGREEMENT\n(ThisAgreement shall remain valid for a period of three (3) years from "
            "the Effective Date.)\nOn this day of,25/06/2025 CSP Name Faiz Hamid Sayyed, CSP Code1A851800")
    r = extract_agreement(text, TODAY)
    assert r["issue_date"] == date(2025, 6, 25) and r["has_explicit_3year_clause"]


JABBAR_HEAD = 'Cusrorurn Srnvtcr Potrur Acneeruerur on this day of 26th May 2025 ("Effective Date"), Jabbar Ansari\n'


@pytest.mark.parametrize("clause, expiry", [
    # Jabbar Ansari, Jharkhand: the period clause sits in 1C, the Term clause says only "until terminated"
    ("C. Eko and CSP have recorded the terms of their relationship for a period of36 months commencing\n"
     "from 26/05/2025 an ending on 25/05/2028.", date(2028, 5, 25)),
    ("the terms of their relationship for a period of 36 months commencing from 26/05/2025.", date(2028, 5, 26)),
    ("This Agreement shall remain in force for a period of thirty-six (36) months.", date(2028, 5, 26)),
    ("commencing from 26/05/2025 and ends on 25/05/2028", date(2028, 5, 25)),
])
def test_agreement_period_in_months_and_ending_on(clause, expiry):
    r = extract_agreement(JABBAR_HEAD + clause + "\nTerm: shall be valid until terminated in accordance with Clause I(4)B",
                          TODAY)
    assert r["issue_date"] == date(2025, 5, 26) and r["explicit_expiry"] == expiry and not r.get("validity_conflict")


def test_printed_end_date_that_disagrees_with_the_stated_period_is_flagged():
    r = extract_agreement(JABBAR_HEAD + "relationship for a period of 36 months commencing from 26/05/2025 "
                                        "and ending on 25/05/2026.", TODAY)
    assert r["explicit_expiry"] == date(2026, 5, 25) and "check which is right" in r["validity_conflict"]


def test_agreement_without_a_stated_validity_expires_after_one_year_and_is_checked(tmp_path):
    """No validity found ('valid until terminated' only): Eko's 1-year rule
    (2026-10-22) makes it expired, to be renewed; a person still checks the term."""
    import pymupdf
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(["CUSTOMER SERVICE POINT AGREEMENT",
                              'On this day of, 12/07/2024 ("Effective Date"), CSP Name Test CSP',
                              "Term: This Agreement shall commence on the Effective Date and shall be valid until "
                              "terminated in accordance with Clause I(4)B.",
                              "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign."]):
        page.insert_text((30, 60 + 20 * i), line, fontsize=8)
    r = extract_document_fields_deterministic(d.tobytes(), "agreement.pdf", allow_llm_fallback=False,
                                              expected_type="AGREEMENT")
    assert r["compliance_status"] == "EXPIRED" and r["expiry_date"] == "2025-07-12" and not r.get("type_review")
    assert r["validity_check"].startswith("Validity check") and "renew" in r["validity_check"]


# ------------------- cases from real uploads (18 Oct 2026), text as OCR read it
def test_bihar_character_certificate_is_valid_six_months():
    """Bihar's online certificate: 'इसकी वैधता निर्गत होने की तिथि से छः माह तक ही है'."""
    hindi = ("Government of Bihar Character Certificate Appl. Ref. No : BCHC/2026/337487 Date: 01/04/2026\n"
             "... इसकी वैधता निर्गत होने की तिथि से छः माह तक ही है।\n"
             "Digitally signed by VINAY KUMAR Date:2026.04.01 01:27:58 +05:30")
    r = extract_pvr(hindi, TODAY)
    assert r["issue_date"] == date(2026, 4, 1) and r["validity_months"] == 6


def test_bihar_bchc_certificate_is_six_months_even_when_ocr_loses_the_hindi():
    garbled = ("Government of Bihar\nOffice of Superintendent of Police\n -/Character Certificate\n"
               "i/ Date: 01/04/2026\n- / Appl. Ref. No : BCHC/2026/337487\n3  (Krishna Kumar), -T , PR -R, -\n"
               "Digitally signed by VINAY KUMAR\nDate:2026.04.01 01:27:58 +05:30")
    r = extract_pvr(garbled, TODAY)
    assert r["validity_months"] == 6 and r["validity_source"] == "PVR_STATED_6_MONTHS_BIHAR_BCHC"


IIBF_FEE_RECEIPT = """INDIAN INSTITUTE OF BANKING & FINANCE
ISO 21001:2018 Certified
www.iibf.org.in
TAX INVOICE CUM RECEIPT
Details of service recipient
ORIGINAL FOR RECIPIENT
Henber no: 802915873
Invoice Nunber: EX/25-26/206540
Menber nane; ASHISH KUMAR
Date: 17-11-2025
GSTIN No: 27AAATT3309D1ZS
Exan code: 1046
Description of Service
Conduction of Exan 999294 800,00"""


def test_iibf_fee_receipt_is_not_the_certificate():
    from app.ocr_service import classify_allowlist_gate
    ok, kind, _, _ = classify_allowlist_gate(IIBF_FEE_RECEIPT, "iibf.pdf", expected_type="IIBF_CERTIFICATE")
    assert not ok and kind == "IIBF_RECEIPT_ONLY"
    cert = ("INDIAN INSTITUTE OF BANKING & FINANCE  Certificate Examination for Business Correspondents\n"
            "This is to certify that ASHISH KUMAR has passed the examination. Certificate No. 123 Date 17-11-2025")
    assert classify_allowlist_gate(cert, "iibf.pdf")[1] == "IIBF_CERTIFICATE"


def test_ordinal_range_with_ocr_quote_marks():
    """'30th December 2023 to 29th December 2026', the 'th' read as quotes."""
    t = ("CUSTOMER SERVICE Point AGREEMENT\n' ROSHAN KUMAR(1A850249)\n"
         "On this day of, 30” December 2023 to 29\" December 2026, an Indian national, son/daughter/wife of")
    r = extract_agreement(t, TODAY)
    assert r["issue_date"] == date(2023, 12, 30) and r["explicit_expiry"] == date(2026, 12, 29)


def test_handwritten_year_two_read_as_nine():
    assert [f.value for f in find_dates("nhis day of, 15-04-9026 CSP Name MUKESH")] == [date(2026, 4, 15)]
    assert find_dates("Rs 9026 paid") == []


def test_ranjeet_36_months_clause_on_page_two():
    t = ("CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 01-June-2024 Ranjeet Kumar Singh, an Indian national\n"
         "C. Eko and CSP have recorded the terms of their relationship for a period of 36 months\n"
         "commencing from 01/06/2024 an ending on 31/05/2027.\n"
         "A. Term: This Agreement shall commence on the Effective Date and shall be valid until\n"
         "terminated in accordance with Clause I(4)B or Clause III(1).")
    r = extract_agreement(t, TODAY)
    assert r["issue_date"] == date(2024, 6, 1) and r["explicit_expiry"] == date(2027, 5, 31)


def test_the_model_is_not_asked_unless_switched_on(monkeypatch):
    import pymupdf
    import app.ai.extraction.deterministic_extractor as ex
    asked = []
    monkeypatch.setattr(ex, "_ask_model", lambda scan, t: asked.append(t))
    d = pymupdf.open()
    d.new_page().insert_text((40, 60), "Some letter that is none of the three documents, no date.", fontsize=9)
    ex.extract_document_fields_deterministic(d.tobytes(), "x.pdf", expected_type="AGREEMENT")
    assert asked == []


def test_this_day_of_with_the_first_letters_under_the_stamp():
    """Mukesh Saini: notary stamp over 'On t', handwritten 2 read as 9."""
    t = ("TTAR PRADESHTOMER ERVICE OINT GREEMENT\n504140\nnhis day of, \n15-04-9026\nCSP NOmc MUKESH SAINI\n"
         "This Agreement shall remain valid for a period of three (3) years from the Effective Date.")
    r = extract_agreement(t, TODAY)
    assert r["issue_date"] == date(2026, 4, 15) and r["has_explicit_3year_clause"]


@pytest.mark.parametrize("line", [
    "No te This agreem ent is Valid from_____ 12/07/2025 _____to_____ 11/07/2028",
    "CIAL\nNo\ns agreemanis Valid from\n12/07/2025\nto\n11/07/2028\n1A185",
    "is Valid from 12/07/2025\n1A85\nto 11/07/2028",
    "is Valid from\n12/07/2025\n\n11/07/2028",
])
def test_anuradha_valid_from_to_under_the_stamp(line):
    t = ('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of 12/07/2025\n("Effective Date"), CSP Name ANURADHA\n' + line
         + "\nTerm: This Agreement shall be valid until terminated in accordance with Clause I(4)B.")
    r = extract_agreement(t, TODAY)
    assert r["issue_date"] == date(2025, 7, 12) and r["explicit_expiry"] == date(2028, 7, 11)


@pytest.mark.parametrize("head", [
    'On this day of, 29/08/2025 ("Effective Date") 27/08/2028 Sunil Kumar Gupta an Indian national',
    'On this day of, 29/08/2025 (\n"Effective Date") 27/08/2028 Sunil Kumar',            # as OCR split it
])
def test_end_date_right_after_effective_date(head):
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\n" + head, TODAY)
    assert r["issue_date"] == date(2025, 8, 29) and r["explicit_expiry"] == date(2028, 8, 27)


def test_effective_date_label_with_the_start_date_is_not_an_end_date():
    r = extract_agreement('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 20/04/2024 ("Effective Date"), '
                          'CSP Name X\nThis Agreement shall remain valid for a period of three (3) years', TODAY)
    assert r["issue_date"] == date(2024, 4, 20) and r["explicit_expiry"] is None and r["has_explicit_3year_clause"]


def test_effected_date_is_the_end_date():
    """Krishna Kumar 1A850973: 'On this day of, 11-06-2026, effected date 10-06-2029'."""
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 11-06-2026, effected date 10-06-2029 "
                          "CSP Name KRISHNA KUMAR CSP Code-", TODAY)
    assert r["issue_date"] == date(2026, 6, 11) and r["explicit_expiry"] == date(2029, 6, 10)


def test_a_valid_agreement_with_a_guessed_validity_gets_a_check(tmp_path):
    import pymupdf
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(["CUSTOMER SERVICE POINT AGREEMENT", 'On this day of, 11/06/2026 ("Effective Date"), CSP Name X',
                              "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign."]):
        page.insert_text((30, 60 + 20 * i), line, fontsize=8)
    r = extract_document_fields_deterministic(d.tobytes(), "a.pdf", allow_llm_fallback=False, expected_type="AGREEMENT",
                                              today=date(2026, 10, 18))
    assert r["compliance_status"] == "VALID" and not r.get("type_review")
    assert r["validity_check"].startswith("Validity check")


# Haryana Police "Character Verification Certificate": the police line's
# "TILL DATED 25.08.2025" is the record-check date; the certificate is issued
# when it is digitally signed, the next day.
HARYANA_CVC = ("HARYANA POLICE CITIZEN SERVICES\nCharacter Verification Certificate\nTo Whomsoever It May Concern\n"
               "The above-mentioned person has applied for Character Verification Certificate on Haryana Police "
               "citizen portal on\n06/a8lzozs\nTFIERE 15 NOTHING ADVERSE AGAII\\ST THE APPLICANT AS PER REPORT OF "
               "POLICE STATION PARTAP NAGAR TILL DATED 25.08.2025\n")


@pytest.mark.parametrize("stamp", [
    "Digitally Signed by:\nKanwal Jeet\nPersonal\nDate: 2025-08-26T12:45:25.8392903+05:30",
    "Digitally Signed by:\nKanwal Jeet\nPersonal\nDate: 2025-08-2612:45:25.8392903+05:30",     # "T" lost
    "Digitally Signed by:\nKanwal Jeet\nPersonal\nDate: 2025-08-26I12:?5:25.8392903+05:30",    # "T" as I, digit hidden
    "Digitally Sianed by:\nKanwal Jeet\nPersonal\nDate: 2025-08-26T12:45:25.8392903+05:30",    # "Signed" misread
])
def test_pvr_haryana_signature_date_even_when_ocr_garbles_it(stamp):
    r = extract_pvr(HARYANA_CVC + stamp, date(2026, 10, 21))
    assert r["issue_date"] == date(2025, 8, 26) and r["date_source"] == "PVR_DIGITAL_SIGNATURE_DATE"
    assert r["validity_months"] == 12 and r["validity_source"] == "PVR_DEFAULT_1_YEAR"


def test_pvr_haryana_without_signature_keeps_the_dated_line():
    r = extract_pvr(HARYANA_CVC, date(2026, 10, 21))
    assert r["issue_date"] == date(2025, 8, 25)


@pytest.mark.parametrize("line", ["3 Years For Valid", "3 Years Valid", "Three (3) years validity", "3 yeers for vaild"])
def test_agreement_heading_n_years_for_valid(line):
    """Sunny Singh: '3 Years For Valid' printed under the title (valid after the term)."""
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement(f'CUSTOMER SERVICE POINT AGREEMENT\n{line}\nOn this day of, 01/07/2026____("Effective Date"), '
                          "SUNNY SINGH\n", date(2026, 10, 24))
    assert r["has_explicit_3year_clause"] and r["validity_source"] == "AGREEMENT_EXPLICIT_3_YEAR"


@pytest.mark.parametrize("line", ["valid for 3 months only", "30 Years For Valid"])
def test_agreement_n_years_for_valid_does_not_misfire(line):
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement(f'CUSTOMER SERVICE POINT AGREEMENT\n{line}\nOn this day of, 01/07/2026____("Effective Date"), X\n',
                          date(2026, 10, 24))
    assert not r["has_explicit_3year_clause"]


BIHAR_PHOTO = ("अथवा सरकारी संस्थाओं से ऋण लेते समय ... से छः माह तक ही है।\nPlace : मधेपुरा / MADHEPURA\nDate : 06/08/2026\n"
               "Digitally signed by Neshar Ahmad Shah\nDate:2O26.O8.O7 l2:OO:l7 +O5:3O\n"
               "पुलिस अधीक्षक / Superintendent of Police\nमधेपुरा / MADHEPURA\n"
               "Reference No: BCHC/2026/995024 To View: https://serviceonline.bihar.gov.in/officials/t/mMjoF\n"
               "vivo Y200 5G\nOct 24, 2026, 17:13\n")


def test_a_phone_camera_stamp_is_never_the_documents_date():
    """Rubi Kumari 1A850634: the photo's 'vivo Y200 / Oct 24, 2026, 17:13' was taken as the signing date."""
    r = extract_pvr(BIHAR_PHOTO, date(2026, 10, 24))
    assert r["issue_date"] != date(2026, 10, 24)
    assert r["issue_date"] in (date(2026, 8, 7), date(2026, 8, 6))


@pytest.mark.parametrize("stamp", ["Shot on Redmi Note 12\n2026/10/24 17:13", "OPPO A78\n24/10/2026 5:13 PM",
                                   "Oct 24, 2026, 17:13"])
def test_camera_stamps_are_removed(stamp):
    from app.ai.extraction.rules.dates import strip_camera_stamps
    assert "2026" not in strip_camera_stamps(stamp)


def test_document_dates_with_times_are_kept():
    from app.ai.extraction.rules.dates import strip_camera_stamps
    for line in ("Certificate Issued Date : 13-Feb-2025 10:10 AM", "Date: 2026.01.28 18:29:17 +05'30'", "24/10/2026 17:13"):
        assert strip_camera_stamps(line) == line


@pytest.mark.parametrize("line", ['On this day of, 7-10-2024 ("Effective Date"), 7-10-2025 Manoj Kumar',
                                  'On tis day of, 7 - 10 - 2o24 ("Effective Date"), 7-1o-2025 Manoj Kumar',
                                  'On this day of, o7-1O-2024 ("Effective Date"), 7-10-2025 Manoj Kumar'])
def test_handwritten_effective_date_then_last_day(line):
    """Manoj Kumar (Gorakhpur): 'On this day of, 7-10-2024 ("Effective Date"), 7-10-2025' in handwriting,
    where OCR reads a 0 as the letter o."""
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement(f"CUSTOMER SERVICE POINT AGREEMENT\n{line}\n", date(2026, 10, 24))
    assert r["issue_date"] == date(2024, 10, 7) and r["explicit_expiry"] == date(2025, 10, 7)


def test_letter_o_is_fixed_only_inside_a_date():
    from app.ai.extraction.rules.dates import normalize
    assert normalize("Room 10 Bazzar Po. KUIN, 7-1o-2025") == "Room 10 Bazzar Po. KUIN, 7-10-2025"


@pytest.mark.parametrize("text,issue,end", [
    ('On this day of 01/11/2025 to 31/12/2028 "Effective Date" PRASHANT KUMAR MEHTA an indian\n', date(2025, 11, 1), date(2028, 12, 31)),
    ('On this day of 24-6-2025 ("Effective Date"), CSP Name DINESH KUMAR\nNote: This agreement is Valid from 24-6-2025 to 23-6-2028\n',
     date(2025, 6, 24), date(2028, 6, 23)),
    ('On this day of 24-6-202S ("Effective Date"), CSP Name DINESH KUMAR\nThis agreement is Valid from 24-6-202S to 23-6-2o28\n',
     date(2025, 6, 24), date(2028, 6, 23)),
    ('On this day of, 04/12/2024 ("Effective Date"), X\nA. Term: This Agreement shall commence on the Effective Date '
     'and shall be valid upto 03/12/2027\nor until terminated\n', date(2024, 12, 4), date(2027, 12, 3)),
    ('4. TERM AND TERMINATION\nA. Term: This Agreement shall commence on the Effective Date and shall be valid upto '
     '03/12/2027\nor until terminated in accordance with Clause I(4)B\n', None, date(2027, 12, 3)),
])
def test_agreement_ranges_and_terms_seen_on_real_documents(text, issue, end):
    """Prashant Kumar Mehta ('day of X to Y'), Dinesh Kumar 1A852416 (handwritten 'Valid from .. to ..',
    5 read as S), and a term page 'valid upto 03/12/2027' whose issue date isn't readable."""
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\n" + text, date(2026, 10, 24))
    assert r["issue_date"] == issue and r["explicit_expiry"] == end


def test_digit_lookalikes_are_fixed_only_inside_a_date():
    from app.ai.extraction.rules.dates import normalize
    assert normalize("SUNNY SINGH, Sec. 44, 24-6-202S") == "SUNNY SINGH, Sec. 44, 24-6-2025"


MANOJ_SAH = ('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 16/01/2025 ("Effective Date"),MANOJ KUMAR SAH CSP Kiosk '
             "Code (1A850757), an Indian national,\nA. Eko and CSP have recorded the terms of their relationship. for a "
             "period of 36 months commencing from\n16/01/2025 an ending on 15/01/2028.\n")


@pytest.mark.parametrize("ending", ["an ending", "an e|ding", "an e\\ding", "an eading", "an endlng"])
def test_period_of_36_months_ending_on_with_a_mark_on_ending(ending):
    """Manoj Kumar Sah 1A850757: a tick over 'ending' made OCR lose it, and 'relationship. for a period of 36
    months' wasn't used either: he was turned away as expired by the 1-year rule."""
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement(MANOJ_SAH.replace("an ending", ending), date(2026, 10, 25))
    assert r["issue_date"] == date(2025, 1, 16)
    assert r["explicit_expiry"] in (date(2028, 1, 15), date(2028, 1, 16))    # printed last day, or 36 months


def test_proceeding_is_not_an_end_date():
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 16/01/2025 ("Effective Date"), X\n'
                          "any proceeding on 15/01/2027 under this clause\n", date(2026, 10, 25))
    assert r["explicit_expiry"] is None


@pytest.mark.parametrize("text", [
    "झारखंड पुलिस गिरिडीह\nसेवा निवेदन रसीद\nअनुरोध का प्रकार चरित्र प्रमाण पत्र का अनुरोध\nअनुरोध संख्या 341102611739\n"
    "आवेदक का नाम JAYNARAYN PRASAD YADAV\nआवेदन स्थिति Registered\nआवेदन करने की तिथि 07/10/2026\n",
    "झारखंड पुलिस\nचरित्र प्रमाण पत्र का अनुरोध\n341102611739\nJAYNARAYN PRASAD YADAV\nRegistered\n07/10/2026\n",
])
def test_jharkhand_service_request_receipt_is_not_a_certificate(text):
    """Jaynarayn Prasad Yadav 1A850583: the Jharkhand Police 'सेवा निवेदन रसीद' (application receipt)."""
    from app.ocr_service import classify_allowlist_gate
    assert classify_allowlist_gate(text, "pvr.jpg", "POLICE_VERIFICATION")[1] == "PVR_APPLICATION_ONLY"


def test_an_issued_certificate_is_still_a_certificate():
    from app.ocr_service import classify_allowlist_gate
    t = ("CHARACTER CERTIFICATE\nThis is to certify that Mr. X ... no adverse entry was found against the said candidate "
         "in the police records. This certificate is valid only for one year.\nRegistered office: Giridih\n")
    assert classify_allowlist_gate(t, "pvr.pdf", "POLICE_VERIFICATION")[1] == "POLICE_VERIFICATION"


# ----------------------------------------- cases refused by mistake, 25-10-2026
@pytest.mark.parametrize("name,text,issue,end", [
    ("Indrajeet Jha 1A850396", "On this day of 23/10/2025 (Effective Date) to 22/10/2028, Indrajeet Jha an\nIndian national",
     date(2025, 10, 23), date(2028, 10, 22)),
    ("Shatrughna Ray 1A850333", 'On this day of 15.06.2025,("Effective Date") CSP Name Mr.SHATRUGHNA RAY\n'
     "is agreement would be Va|d for Two Years from the date of signing .", date(2025, 6, 15), date(2027, 6, 15)),
    ("Roshan Kumar 1A850249", "ROSHAN KUMAR(1A850249)\nOn this day of, 30th December 2023 to 29th December 2026, an Indian national",
     date(2023, 12, 30), date(2026, 12, 29)),
])
def test_agreements_refused_by_mistake_now_read(name, text, issue, end):
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT\n" + text + "\n", date(2026, 10, 26))
    assert (r["issue_date"], r["explicit_expiry"]) == (issue, end), name


def test_three_years_from_the_date_of_signing():
    from app.ai.extraction.rules.agreement import extract_agreement
    r = extract_agreement('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 30/12/2024 ("Effective Date"), X\n'
                          "This agreement would be valid for three years from the date of signing.\n", date(2026, 10, 26))
    assert r["has_explicit_3year_clause"]


MH_PCC_TO_EKO = ("POLICE CLEARANCE CERTIFICATE\nTo,\nBANK BC CUSTOMER SERVICE POINT\nEKO BHARAT VENTURE PVT LTD\n"
                 "Subject : Verification of Character and Antecedents of BHUSHA RAMESH NANDRE residing at Chhadvel\n"
                 "There is nothing adverse against the above applicant on police record ... report dated 07/02/2026\n"
                 "Signature valid\nDigitally Signed by\nChandrakant Vasantrao Khonde\nDate:24/4/2026 5:57:19 PM\n"
                 "For Superintendent of Police.\n")


def test_police_clearance_addressed_to_eko_csp_is_a_police_certificate_valid_one_year():
    """Bhushan Nandre 1A851806: 'To, BANK BC CUSTOMER SERVICE POINT, EKO ...' made it look like an agreement;
    a Police Clearance Certificate that states no validity is valid 1 year (since 26-10-2026)."""
    from app.ocr_service import classify_allowlist_gate
    assert classify_allowlist_gate(MH_PCC_TO_EKO, "pvr.pdf", "POLICE_VERIFICATION")[1] == "POLICE_VERIFICATION"
    r = extract_pvr(MH_PCC_TO_EKO, date(2026, 10, 26))
    assert r["validity_months"] == 12 and r["validity_source"] == "PVR_DEFAULT_1_YEAR"


def test_an_agreement_still_reads_as_an_agreement():
    from app.ocr_service import classify_allowlist_gate
    t = ('CUSTOMER SERVICE POINT AGREEMENT\nOn this day of, 01/01/2026 ("Effective Date"), X\nEko hereby appoints X.\n'
         "CSP shall submit police verification and character certificate. nothing adverse\n")
    assert classify_allowlist_gate(t, "a.pdf", "AGREEMENT")[1] == "AGREEMENT"


# A notary seal over "a period of three (3) years" in the heading: OCR kept
# only "remain valid for a from the Effective Date" (Gurupad Mahato 1A852377).
SEALED_HEADING = ("CUSTOMER SERVICE POINT AGREEMENT (This Agreeme nt shall r*main valid for a from the Effective Date.) "
                  "On this day of, 01 October 2026, CSP Name GURUPAD MAHATO CSP Code LA852377 an lndian national")
DEPOSIT_CLAUSE = ("This security deposit shall be refunded in all cases where CSP terminates the Agreement in accordance "
                  "with Clause I(4)B after providing 36 months of continuous Services to customers. B. If, within 36 "
                  "months of Effective Date, CSP terminates the Agreement in accordance with Clause I(4)B, a minimum "
                  "penalty as mentioned in Clause 3 of Annexure 2 will apply to CSP's security deposit.")


def test_validity_hidden_under_a_seal_is_flagged_not_read_as_a_period():
    r = extract_agreement(SEALED_HEADING + " " + DEPOSIT_CLAUSE, today=date(2026, 10, 28))
    assert r["issue_date"] == date(2026, 10, 1) and r["validity_source"] == "AGREEMENT_DEFAULT_1_YEAR"
    assert r["validity_hidden"] and not r["explicit_expiry"]


@pytest.mark.parametrize("heading, years", [
    ("(This Agreement shall remain valid for a period of three (3) years from the Effective Date.)", 3),
    ("(This Agreement shall remain valid for a pen od of Wee (3) years from the Effective Date.)", 3),
    ("(This Agreement shall remain valid for a period of two (2) years from the Effective Date.)", 2),
])
def test_a_readable_validity_heading_is_not_called_hidden(heading, years):
    r = extract_agreement(f"CUSTOMER SERVICE POINT AGREEMENT {heading} On this day of, 01 October 2026, CSP Name X "
                          + DEPOSIT_CLAUSE, today=date(2026, 10, 28))
    assert not r.get("validity_hidden")
    if years == 3:
        assert r["has_explicit_3year_clause"]
    else:
        assert r["explicit_expiry"] == date(2028, 10, 1)


def test_the_deposit_clause_alone_is_neither_a_period_nor_hidden():
    r = extract_agreement("CUSTOMER SERVICE POINT AGREEMENT On this day of, 01 October 2026, CSP Name X " + DEPOSIT_CLAUSE,
                          today=date(2026, 10, 28))
    assert r["validity_source"] == "AGREEMENT_DEFAULT_1_YEAR" and not r["validity_hidden"]
    assert not r["explicit_expiry"] and not r["has_explicit_3year_clause"]


def test_a_hidden_validity_gets_its_own_check_and_explanation():
    import pymupdf
    from app.ai.extraction.deterministic_extractor import extract_document_fields_deterministic
    from app.why import explain
    d = pymupdf.open()
    page = d.new_page()
    for i, line in enumerate(["CUSTOMER SERVICE POINT AGREEMENT",
                              "(This Agreement shall remain valid for a from the Effective Date.)",
                              'On this day of, 01/10/2026 ("Effective Date"), CSP Name X',
                              "Eko hereby appoints the CSP. Security deposit. In witness whereof the parties sign."]):
        page.insert_text((30, 60 + 20 * i), line, fontsize=8)
    r = extract_document_fields_deterministic(d.tobytes(), "a.pdf", allow_llm_fallback=False, expected_type="AGREEMENT",
                                              today=date(2026, 10, 28))
    assert r["compliance_status"] == "VALID" and r["expiry_date"] == "2027-10-01" and r["validity_hidden"]
    assert r["validity_check"].startswith("Validity check: the validity sentence is there but its number is hidden")
    how = next(x for x in explain(r)["lines"] if x["what"] == "Valid till")["how"]
    assert "hidden under a stamp" in how and "no validity written" not in how


# Mitan Prasad Verma (1A850197), DC link, 28-10-2026: the scan read "day of" as
# "day ol", "36" as "t16" and the slashes of the end date as "I". It was put
# under the date the DC typed (25-05-2025) + 1 year instead of 26-05-2025 -> 25-05-2028.
MITAN = ('--- Page 2 --- CusrouER Senvrce Porrur AcRrrvrrur On this day ol 26th May 2025("Effective Date"), Mitan Prasad '
         'Verma, an lndian national, son/daughter of X. Eko and CSP have recorded the terms of their relationship for a '
         'period oft16 months commencing\nfrom 26/ 05/ 2025 an ending on 25 I 05 12028.\n2.\nREMUNERATIoN\nA. Eko shall')


def test_scan_damaged_dates_and_period_are_read_right():
    r = extract_agreement(MITAN, today=date(2026, 10, 28))
    assert (r["issue_date"], r["date_source"]) == (date(2025, 5, 26), "AGREEMENT_THIS_DAY_OF")
    assert r["explicit_expiry"] == date(2028, 5, 25) and not r.get("validity_conflict")   # "t16" is not a period


@pytest.mark.parametrize("text, expected", [
    ("25 I 05 I 2028", date(2028, 5, 25)), ("25 l 05/ 2028", date(2028, 5, 25)), ("25 | 05 | 2028", date(2028, 5, 25)),
    ("25 I 05 12028", date(2028, 5, 25)),           # the second slash read as a "1" glued to the year
    ("25 I 13 I 2028", None),                       # no month 13: left alone
    ("25 I 05 12028 4", date(2028, 5, 25)), ("Rs 12 I 05 120281", None),
])
def test_slashes_read_as_letters(text, expected):
    from app.ai.extraction.rules.dates import normalize
    assert [f.value for f in find_dates(text)] == ([expected] if expected else [])
    assert len(normalize(text)) == len(text)        # same length, so positions in the text stay right


@pytest.mark.parametrize("wording, months", [
    ("the terms of their relationship for a period oft16 months", None),
    ("the terms of their relationship for a period of36 months", 36),
    ("the terms of their relationship for a period of 36 months", 36),
    ("This agreement is valid for thirty-six (36) months", 36),
])
def test_a_period_number_glued_to_a_stray_letter_is_not_read(wording, months):
    from app.ai.extraction.rules.agreement import stated_months
    assert stated_months(wording) == months


# Devendra Kumar Daulat 1A850456 (DC link, 30-10-2026): Tesseract left the whole
# line out (a round stamp beside it, a grey lined background); the second OCR
# engine (RapidOCR) read it as below. The body says "two years".
DEVENDRA_RAPIDOCR = ("8.1000\nRs.1000\nINDIA\n21.2510618024-9000x9=9000=\nCUSTOMER SERVICE POINT AGREEMENT\n2236H1\n"
                     "DEVENDRA KUMAR DAULAT(1A850456)\n25106|2084\n20\n"
                     "On tpis day of, 30 December 2023 to 29th December 2026, an Indian national, son/daughter/wife of\n"
                     "LAtIPRASAD DAULAT, residing at SAMHAR KHURD UNDER DISTT-SAHARSA, BIHAR.\n")
DEVENDRA_BODY = "SP of Eko at the said CSP Location. This agréement would be valid for two years from the date of signing."


def test_range_on_the_this_day_of_line_read_by_the_second_engine():
    from app.ai.extraction.rules import extract_agreement
    r = extract_agreement(DEVENDRA_RAPIDOCR, date(2026, 10, 30))
    assert r["issue_date"] == date(2023, 12, 30) and r["explicit_expiry"] == date(2026, 12, 29)
    assert r["validity_source"] == "AGREEMENT_RANGE_FROM_TO" and not r.get("validity_conflict")
    # "tpis" is "this" under the stamp.
    r = extract_agreement("On tpis day of, 30 December 2023 (Effective Date)", date(2026, 10, 30))
    assert r["issue_date"] == date(2023, 12, 30) and r["date_source"] == "AGREEMENT_THIS_DAY_OF"


def test_a_printed_range_that_disagrees_with_the_stated_years_goes_to_a_person():
    from app.ai.extraction.rules import extract_agreement
    r = extract_agreement(DEVENDRA_RAPIDOCR + DEVENDRA_BODY, date(2026, 10, 30))
    assert r["explicit_expiry"] == date(2026, 12, 29)                      # the printed last day counts
    assert "prints 29-12-2026 as its last day" in r["validity_conflict"] and "30-12-2025" in r["validity_conflict"]


def test_the_second_engine_reads_the_page_whole_when_no_line_was_found(monkeypatch):
    from types import SimpleNamespace
    from app.ai.extraction import deterministic_extractor as de
    tesseract = ("son/dalighterPuareer\n; D \\T, residing at SAMHAR KHURD UNDER DISTT-SAHARSA, BIHAR.\n"
                 "Eko india Services Pvt. Ltd. a company incorporated under the Companies Act of india, 1956\n")
    monkeypatch.setattr(de, "focused_reread", lambda scan, t: "")          # no anchor line: nothing to re-read
    monkeypatch.setattr(de, "second_engine_pages", lambda scan: DEVENDRA_RAPIDOCR)
    scan = SimpleNamespace(pages=[SimpleNamespace(source="OCR")])
    today = date(2026, 10, 30)
    r0 = de._rules_for("AGREEMENT", tesseract, today)
    assert r0["issue_date"] is None
    r, text, note = de._second_look(scan, "AGREEMENT", tesseract, r0, today)
    assert r["issue_date"] == date(2023, 12, 30) and r["explicit_expiry"] == date(2026, 12, 29)
    assert "--- second OCR engine ---" in text and note is None
    # Nothing new from the second engine either: the first reading stands.
    monkeypatch.setattr(de, "second_engine_pages", lambda scan: "")
    assert de._second_look(scan, "AGREEMENT", tesseract, r0, today)[0] is r0


# Shailendra Pandit (term clause, page 1 of 12, OCR exact): two-digit years.
SHAILENDRA = ("B. Eko hereby appoints SHAILENDRA PANDIT as a Customer Service Point of\n"
              "Eko at KAMTA PO KAMTA PS HILSA DIST NALANDA, BIHAR-801302\n"
              "C. Eko and CSP have recorded the terms of their relationship. For a period of 36 months\n"
              "commencing from 24/07/24 And ending on 23/07/27\n2. ROLE AND oF CSP\n")


def test_commencing_and_ending_dates_with_two_digit_years():
    r = extract_agreement(SHAILENDRA, date(2026, 10, 30))
    assert r["issue_date"] == date(2024, 7, 24) and r["date_source"] == "AGREEMENT_RANGE_START"
    assert r["explicit_expiry"] == date(2027, 7, 23) and r["validity_source"] == "AGREEMENT_RANGE_FROM_TO"
    assert not r.get("validity_conflict")                     # 36 months stated: agrees
    # The wrong number of months stated is flagged for a person.
    r = extract_agreement(SHAILENDRA.replace("36 months", "12 months"), date(2026, 10, 30))
    assert r["validity_conflict"] and "23-07-2027" in r["validity_conflict"]


def test_a_lone_two_digit_year_is_still_not_guessed():
    for t in ("commencing from 24/07/24 for the agreement", "ending on 23/07/27",
              "commencing from 24/07/99 And ending on 23/07/02",          # outside 2010..
              "commencing from 24/07/24 And ending on 23/07/24"):         # under 30 days
        r = extract_agreement(t, date(2026, 10, 30))
        assert r["issue_date"] is None or r["explicit_expiry"] is None, t


# Prity Kumari (OCR exact): a renewal signed in advance, its term starts after today.
PRITY = ("Eko hereby appoints PRITY KUMARI as a Customer Service Point of Eko at VILL - ENAYATPUR PARBODHI),\n"
         "and hereby agrees to be a CSP of Eko at the said CSP Location.\n"
         "—ko and CSP have recorded the terms of their relationship for a Period of 36 months commencing from\n"
         "01/11/2026 and ending on 30/10/2029.\nROLE AND RESPONSIBILITIES OF CSP\n")


def test_the_start_of_the_term_is_the_issue_date_even_a_few_days_ahead():
    r = extract_agreement(PRITY, date(2026, 10, 30))
    assert r["issue_date"] == date(2026, 11, 1) and r["date_source"] == "AGREEMENT_COMMENCING_DATE"
    assert r["explicit_expiry"] == date(2029, 10, 30) and not r.get("validity_conflict")
    # Over 60 days ahead is not a start date the agent takes.
    r = extract_agreement(PRITY, date(2026, 8, 15))
    assert r["issue_date"] is None
    # A date given by the heading or "On this day of" still comes first.
    r = extract_agreement("On this day of, 28/10/2026 (Effective Date)\n" + PRITY, date(2026, 10, 30))
    assert r["issue_date"] == date(2026, 10, 28) and r["date_source"] == "AGREEMENT_THIS_DAY_OF"
