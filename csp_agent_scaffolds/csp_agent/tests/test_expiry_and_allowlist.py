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


def test_agreement_expiry_default_two_years():
    """Agreement that states no validity defaults to 2 years (business rule)."""
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
    assert result["validity_rule_used"] == ExpiryRule.AGREEMENT_DEFAULT_2_YEAR
    assert result["calculated_expiry"] == date(2027, 4, 1)


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
