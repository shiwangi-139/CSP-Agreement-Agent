"""
tests/test_email_classification.py
Automated tests for deterministic CSP email subject classification and KO code extraction.
"""
import pytest
from app.models import EmailCategory
from app.email_ingest import classify_email


def test_classify_terminal_reset():
    subjects = [
        "Request to terminal reset for KO 1A850247",
        "Request for terminal reset - KO 1A850247",
        "Terminal reset request for KO 1A850247",
        "Urgent: Reset of terminal KO 2B990001",
        "Request to terminal reset for KO"
    ]
    for sub in subjects:
        res = classify_email(sub, body="Please reset the terminal asap", sender="csp@test.com", email_id="msg_001")
        assert res["category"] == EmailCategory.TERMINAL_RESET
        assert res["confidence"] >= 0.95
        assert not res["ai_used"]
        assert res["audit"]["final_status"] == "CLASSIFIED"


def test_classify_terminal_extension():
    subjects = [
        "Request to terminal extension for KO 1A850247",
        "Terminal extension request - KO 1A850247",
        "Extend terminal KO 3C445566",
        "Request for extension of terminal"
    ]
    for sub in subjects:
        res = classify_email(sub, body="Requesting 1 month extension", sender="csp@test.com", email_id="msg_002")
        assert res["category"] == EmailCategory.TERMINAL_EXTENSION
        assert res["confidence"] >= 0.95
        assert not res["ai_used"]


def test_ko_code_extraction():
    res = classify_email("Request to terminal reset for KO 1A850247", email_id="msg_003")
    assert res["extracted_ko"] == "1A850247"
    assert res["audit"]["extracted_ko"] == "1A850247"


def test_classify_pvr_document():
    res = classify_email("Submission of Police Verification Certificate", body="Attached is the PVR for 2026", email_id="msg_004")
    assert res["category"] == EmailCategory.PVR_DOCUMENT
    assert res["rule"] == "KEYWORD_PVR"


def test_classify_character_certificate():
    res = classify_email("Character Certificate for CSP Kiosk Renewal", email_id="msg_005")
    assert res["category"] == EmailCategory.CHARACTER_CERTIFICATE
    assert res["rule"] == "KEYWORD_CHARACTER_CERT"


def test_classify_iibf_document():
    res = classify_email("IIBF Exam Passing Certificate Attached", email_id="msg_006")
    assert res["category"] == EmailCategory.IIBF_DOCUMENT
    assert res["rule"] == "KEYWORD_IIBF"


def test_classify_agreement_document():
    res = classify_email("Signed Renewal Agreement on Rs 100 Stamp Paper", email_id="msg_007")
    assert res["category"] == EmailCategory.AGREEMENT_DOCUMENT
    assert res["rule"] == "KEYWORD_AGREEMENT"


def test_classify_general_communication():
    res = classify_email("Help needed regarding kiosk software issue", email_id="msg_008")
    assert res["category"] == EmailCategory.GENERAL_CSP_COMMUNICATION


def test_classify_fallback_audit():
    res = classify_email("Random unrelated greeting without any keywords", email_id="msg_009")
    assert res["category"] == EmailCategory.UNKNOWN
    assert res["audit"]["final_status"] == "NEEDS_AI_OR_MANUAL_REVIEW"

