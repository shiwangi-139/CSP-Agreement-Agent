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

