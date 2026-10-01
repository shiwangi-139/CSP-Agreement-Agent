import pytest
from datetime import date
from app.expiry_engine import calculate_document_expiry, ExpiryRule
from app.ocr_service import classify_allowlist_gate, check_explicit_3year_clause
from app.storage import get_human_readable_download_name


def test_agreement_expiry_explicit_three_year():
    """Agreement with explicit 3-year text must yield issue_date + 3 years."""
    issue = date(2025, 4, 1)
    ocr_text = "This Customer Service Point Agreement shall remain valid for a period of 3 years from the date of execution."
    has_3yr = check_explicit_3year_clause(ocr_text)
    assert has_3yr is True
    
    result = calculate_document_expiry(
        doc_type="AGREEMENT",
        issue_date=issue,
        has_explicit_3year_clause=has_3yr,
        today=date(2026, 1, 1)
    )
    
    assert result["status"] == "VALID"
    assert result["validity_rule_used"] == ExpiryRule.AGREEMENT_EXPLICIT_3_YEAR
    assert result["calculated_expiry"] == date(2028, 4, 1)


def test_agreement_expiry_default_one_year():
    """Agreement that states no validity defaults to 1 year (business rule since 2026-09-30)."""
    issue = date(2025, 4, 1)
    ocr_text = "Standard Customer Service Point Agreement entered into between Eko and CSP."
    has_3yr = check_explicit_3year_clause(ocr_text)
    assert has_3yr is False
    
    result = calculate_document_expiry(
        doc_type="AGREEMENT",
        issue_date=issue,
        has_explicit_3year_clause=has_3yr,
        today=date(2025, 6, 1)
    )
    
    assert result["status"] == "VALID"
    assert result["validity_rule_used"] == ExpiryRule.AGREEMENT_DEFAULT_1_YEAR
    assert result["calculated_expiry"] == date(2026, 4, 1)


def test_pvr_character_certificate_expiry_one_year():
    """Police Verification and Character Certificate MUST have 1 year validity from issue date."""
    issue = date(2025, 7, 15)
    result_pvr = calculate_document_expiry("POLICE_VERIFICATION", issue, today=date(2025, 8, 1))
    assert result_pvr["status"] == "VALID"
    assert result_pvr["validity_rule_used"] == ExpiryRule.PVR_DEFAULT_1_YEAR
    assert result_pvr["calculated_expiry"] == date(2026, 7, 15)

    result_cc = calculate_document_expiry("CHARACTER_CERTIFICATE", issue, today=date(2025, 8, 1))
    assert result_cc["status"] == "VALID"
    assert result_cc["validity_rule_used"] == ExpiryRule.PVR_DEFAULT_1_YEAR
    assert result_cc["calculated_expiry"] == date(2026, 7, 15)


def test_iibf_certificate_lifetime_no_expiry():
    """IIBF Certificate has LIFETIME validity and MUST NOT have an artificial expiry date."""
    issue = date(2024, 1, 10)
    result = calculate_document_expiry("IIBF_CERTIFICATE", issue)
    
    assert result["status"] == "VALID"
    assert result["validity_rule_used"] == ExpiryRule.IIBF_LIFETIME_NO_EXPIRY
    assert result["calculated_expiry"] is None


def test_unauthorized_document_type():
    """Unauthorized documents must yield unknown document rule."""
    issue = date(2025, 1, 1)
    result = calculate_document_expiry("AADHAAR", issue)
    assert result["status"] == "NEEDS_REVIEW"
    assert result["validity_rule_used"] == ExpiryRule.UNKNOWN_DOCUMENT
    assert result["calculated_expiry"] is None


def test_ocr_allowlist_prefilter():
    """Pre-filter must reject Aadhaar, PAN, Bank Statements, Resumes, Offer Letters."""
    # Unauthorized
    assert classify_allowlist_gate("Aadhaar card issued by Unique Identification Authority of India UIDAI")[0] is False
    assert classify_allowlist_gate("Income Tax Department Permanent Account Number PAN card")[0] is False
    assert classify_allowlist_gate("State Bank of India Statement of Account for period 01/01/2025 to 31/03/2025")[0] is False
    assert classify_allowlist_gate("Offer Letter for employment as Software Engineer Intern")[0] is False
    
    # Authorized
    allowed, doc_type, reason, conf = classify_allowlist_gate("CUSTOMER SERVICE POINT AGREEMENT INDIA NON JUDICIAL Government of Uttar Pradesh")
    assert allowed is True
    assert doc_type == "AGREEMENT"
    
    allowed, doc_type, reason, conf = classify_allowlist_gate("POLICE VERIFICATION REPORT Character Certificate SSP Office Lucknow")
    assert allowed is True
    assert doc_type == "POLICE_VERIFICATION"
    
    # Hindi Character Certificate
    allowed, doc_type, reason, conf = classify_allowlist_gate("कार्यालय पुलिस अधीक्षक चरित्र प्रमाण पत्र निर्गत तिथि")
    assert allowed is True
    assert doc_type == "POLICE_VERIFICATION"
    
    allowed, doc_type, reason, conf = classify_allowlist_gate("INDIAN INSTITUTE OF BANKING & FINANCE IIBF BC/BF Examination Certificate")
    assert allowed is True
    assert doc_type == "IIBF_CERTIFICATE"


def test_human_readable_download_naming():
    """Download filenames must be descriptive and human readable."""
    name = get_human_readable_download_name(
        csp_code="10001",
        csp_name="Shiwangi Sinha",
        doc_type="AGREEMENT",
        year=2025,
        ext="pdf"
    )
    assert name == "10001_Shiwangi_Sinha_CSP_Agreement_2025.pdf"


def test_character_certificate_application_receipt_is_not_a_pvr():
    # UP Police "Service Request Receipt": proof of applying, not the certificate.
    from app.ocr_service import classify_allowlist_gate
    receipt = ("उत्तर प्रदेश पुलिस Service Request Receipt Request Type : CHARACTER CERTIFICATE "
               "Request No. : 319532439031 Name of Applicant : Kaushar Jahan Submited To : विजय नगर Police Station "
               "Current Status : थाना पर दिनांक 08/11/2024 17:03:38 को प्राप्त Date of Submission : 08/11/2024 "
               "Officer-In-Charge")
    ok, doc_type, reason, _ = classify_allowlist_gate(receipt)
    assert not ok and doc_type == "PVR_APPLICATION_ONLY" and "not the certificate" in reason


def test_issued_character_certificate_is_still_a_pvr():
    from app.ocr_service import classify_allowlist_gate
    cert = ("Character Certificate Certificate No. - 316542516797 Date- 29/05/2025 It is certified that "
            "Mr./Miss/Mrs. Sohit Kumar ... no adverse entry was found against the said candidate in the police "
            "records. This certificate is valid only for one year. Crime and Criminal Tracking Network and Systems (CCTNS)")
    ok, doc_type, _, _ = classify_allowlist_gate(cert)
    assert ok and doc_type == "POLICE_VERIFICATION"


def test_character_certificate_challan_receipt_is_not_a_pvr():
    # UP Police fee receipt (1A850168): paid Rs.50, no certificate yet.
    from app.ocr_service import classify_allowlist_gate
    challan = ("Service Requested For Character Certificate Service Request No. 316422637242 Challan No. POL263839878 "
               "Challan Reference No. CPAHFIEVV4 Bank Transaction Date & Time 07 September 2026 Bank Transaction Status "
               "Success Challan Amount Rs.50 Head of Account 005500103030000-Character Character Certificate Challan "
               "Receipt This is a computer generated document and does not require any signature. "
               "Crime and Criminal Tracking Network and Systems (CCTNS) 07/09/2026")
    ok, doc_type, _, _ = classify_allowlist_gate(challan)
    assert not ok and doc_type == "PVR_APPLICATION_ONLY"


def test_haryana_character_verification_request_form_is_not_a_pvr():
    from app.ocr_service import classify_allowlist_gate
    form = ("HARYANA POLICE GENERAL VERIFICATION SERVICES CHARACTER VERIFICATION REQUEST UID/Adhar No: 518446810115 "
            "Appplicant Name (आवेदक का नाम): Chander pal Purpose for Applying: for SBI CSP Mode of Receiving: Wireless")
    ok, doc_type, _, _ = classify_allowlist_gate(form)
    assert not ok and doc_type == "PVR_APPLICATION_ONLY"


def _gate(text):
    from app.ocr_service import classify_allowlist_gate
    return classify_allowlist_gate(text)[1]


def test_agreement_with_garbled_heading_is_not_a_pvr():
    # 13-page agreements on stamp paper, heading garbled by OCR; the body's
    # "obtain their police verification" clause made them look like a PVR.
    text = ("IR he Rs100 HUND INDIA NON JUDICIALTS UTTAR PRADESH STOMER ERVICE OINT GREEMENT On this day of, "
            "10/08/2023 (Effective Date) Eko hereby appoints ... I. RELATIONSHIP DEFINITION ... CSP shall ensure "
            "that due diligence is done on employees and also obtain their police verification before their "
            "appointment. ... (Initials of Eko) (Initials of CSP) ... 5. SECURITY DEPOSIT ... Annexure 2")
    assert _gate(text) == "AGREEMENT"


def test_real_certificates_from_several_states_stay_pvr():
    up = ("Character Certificate Certificate No. - 316542516797 Date- 29/05/2025 It is certified that Mr. Sohit Kumar "
          "no adverse entry was found ... valid only for one year. Crime and Criminal Tracking Network and Systems (CCTNS)")
    bihar = ("Government of Bihar Office of Superintendent of Police District : NAWADA Character Certificate "
             "Date: 15/12/2025 This is to certify that ... nothing adverse ... police records")
    delhi = ("OFFICE OF THE DEPUTY COMMISSIONER OF POLICE: SPECIAL BRANCH DELHI POLICE BHAWAN "
             "POLICE CLEARANCE CERTIFICATE This is to certify that no adverse report ... Date 02/06/2026")
    assert _gate(up) == _gate(bihar) == _gate(delhi) == "POLICE_VERIFICATION"


def test_application_forms_and_receipts_from_several_states_are_rejected():
    forms = [
        # Delhi Police PCC application (still under verification)
        "SPECIAL BRANCH DELHI POLICE BHAWAN POLICE CLEARANCE CERTIFICATE (PCC) APPLICATION FORM APPLICATION "
        "NUMBER :: DLSB- PCC/202606020019 STATUS: UNDER MODE OF VERIFICATION : ADDRESS AND VERIFICATION",
        # Haryana (OCR wrote RECEIPE)
        "HARYANA POLICE VERI FIC ATION SERVICES Character Certificate APPLICATION RECEIPE Name: REETU KAMBOI "
        "Application No.: 1323126608742",
        # UP online request
        "Receiving Receipt CHARACTER CERTIFICATE REQUEST Form No. 202503277853 Apply Date 27-03-2025 Apply Time "
        "09:28:37 District Lucknow Reason for Application CSP CENTER HETU",
        # Maharashtra
        "OFFICE OF THE COMMISSIONER OF POLICE,THANE Application cum Personal Particulars Form for Character & "
        "Antecedents Verification Applicant's Name SONAWANE PRAKASH Application ID NDBR01261000709",
    ]
    assert [_gate(f) for f in forms] == ["PVR_APPLICATION_ONLY"] * 4


def test_bank_due_diligence_form_is_not_a_pvr():
    text = ("Branch Name & Code: SBI FORMAT FOR DUE DILIGENCE REPORT AND KYC VERIFICATION ENGAGEMENT OF CSP/SUB AGENT "
            "Name of proposed CSP RAJNI DEVI ... Verification Details: Police verification done")
    assert _gate(text) == "NOT_A_PVR"
