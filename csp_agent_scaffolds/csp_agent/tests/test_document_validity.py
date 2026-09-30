"""
tests/test_document_validity.py
Tests for centralized document validity rules and state-specific PVR routing.
- PVR: 1 year validity
- Character Certificate: 3 years validity
- IIBF: Permanent record (no expiry)
"""
from datetime import date
from app.ai.extraction.deterministic_extractor import add_years
from app.ai.extraction.pvr_registry import get_pvr_parser, BasePVRParser


def test_add_years_calculation():
    issue = date(2026, 3, 15)
    
    # 1. PVR validity is 1 year
    pvr_expiry = add_years(issue, 1)
    assert pvr_expiry == date(2027, 3, 15)
    
    # 2. Character Certificate validity is 3 years
    cc_expiry = add_years(issue, 3)
    assert cc_expiry == date(2029, 3, 15)
    
    # 3. Leap year handling
    leap_date = date(2024, 2, 29)
    leap_expiry = add_years(leap_date, 1)
    assert leap_expiry == date(2025, 2, 28)


def test_pvr_registry_state_routing():
    # UP State parser
    up_parser = get_pvr_parser("UTTAR PRADESH")
    assert up_parser is not None
    assert up_parser.state_name == "UTTAR PRADESH"
    
    # Bihar parser
    bihar_parser = get_pvr_parser("BIHAR")
    assert bihar_parser is not None
    assert bihar_parser.state_name == "BIHAR"
    
    # Default fallback parser for other states
    generic_parser = get_pvr_parser("UNKNOWN_STATE")
    assert isinstance(generic_parser, BasePVRParser)


def test_pvr_expiry_date_computation():
    parser = get_pvr_parser("UTTAR PRADESH")
    issue = date(2026, 1, 10)
    exp = parser.calculate_expiry(issue)
    assert exp == date(2027, 1, 10)



def test_police_clearance_certificate_without_validity_is_lifetime():
    from datetime import date
    from app.ai.extraction.rules.pvr import extract_pvr
    pcc = ("POLICE CLEARANCE CERTIFICATE  PCC No. 4521/2023  Date: 14/03/2023  This is to certify that "
           "Sh. Ram Pal s/o Sh. Mohan Lal has no criminal record in this police station.")
    r = extract_pvr(pcc, date(2026, 9, 29))
    assert r["issue_date"] == date(2023, 3, 14) and r["validity_source"] == "PVR_PCC_LIFETIME"


def test_pvr_with_stated_validity_keeps_it_even_if_it_says_pcc():
    from datetime import date
    from app.ai.extraction.rules.pvr import extract_pvr
    text = ("Police Clearance Certificate  Date: 14/03/2025  no adverse entry ... "
            "This certificate is valid for six months only.")
    assert extract_pvr(text, date(2026, 9, 29))["validity_months"] == 6


def test_pcc_expiry_is_lifetime():
    from datetime import date
    from app.expiry_engine import calculate_document_expiry
    r = calculate_document_expiry("POLICE_VERIFICATION", date(2023, 3, 14), validity_rule="PVR_PCC_LIFETIME")
    assert r["status"] == "VALID" and r["calculated_expiry"] is None
