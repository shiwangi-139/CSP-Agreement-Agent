"""
app/ai/extraction/pvr_registry.py
State-specific Police Verification Report (PVR) parser registry.
Configurable architecture:
- BasePVRParser (Generic 1-year calculation, common patterns)
- State-specific parsers (UP, Bihar, Maharashtra, Rajasthan, etc.)
- Fallback routing for unknown states with AI/Manual review flags.
"""
import re
import logging
from datetime import date
from typing import Optional, Dict, Any

from .deterministic_extractor import add_years

logger = logging.getLogger(__name__)


class BasePVRParser:
    """Base PVR parser applying regulatory 1-year validity rule."""
    state_name = "GENERIC"

    def calculate_expiry(self, issue_date: date) -> date:
        return add_years(issue_date, 1)

    def parse(self, text: str) -> Dict[str, Any]:
        result = {"state": self.state_name, "validity_years": 1}
        # Generic date matching
        date_match = re.search(r'(?:valid\s+till|expiry\s+date|validity)\s*[:#-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})', text, re.IGNORECASE)
        if date_match:
            result["police_verification_valid_until"] = date_match.group(1)
        issue_match = re.search(r'(?:date\s+of\s+issue|issued\s+on|dated)\s*[:#-]?\s*(\d{2}[-/]\d{2}[-/]\d{4})', text, re.IGNORECASE)
        if issue_match:
            result["police_verification_date"] = issue_match.group(1)
        return result


class UttarPradeshPVRParser(BasePVRParser):
    state_name = "UTTAR PRADESH"

    def parse(self, text: str) -> Dict[str, Any]:
        result = super().parse(text)
        result["state"] = self.state_name
        result["issuing_authority"] = "Uttar Pradesh Police"
        return result


class BiharPVRParser(BasePVRParser):
    state_name = "BIHAR"

    def parse(self, text: str) -> Dict[str, Any]:
        result = super().parse(text)
        result["state"] = self.state_name
        result["issuing_authority"] = "Bihar Police"
        return result


# Configurable State Registry
PVR_PARSER_REGISTRY: Dict[str, BasePVRParser] = {
    "UTTAR PRADESH": UttarPradeshPVRParser(),
    "UP": UttarPradeshPVRParser(),
    "BIHAR": BiharPVRParser(),
}

DEFAULT_PARSER = BasePVRParser()


def get_pvr_parser(state: Optional[str]) -> BasePVRParser:
    """Retrieves state-specific PVR parser from registry or returns default generic parser."""
    if not state:
        return DEFAULT_PARSER
    norm_state = state.strip().upper()
    return PVR_PARSER_REGISTRY.get(norm_state, DEFAULT_PARSER)


def register_pvr_parser(state_name: str, parser_instance: BasePVRParser):
    """Allows runtime extension of new state formats without pipeline rewrites."""
    PVR_PARSER_REGISTRY[state_name.strip().upper()] = parser_instance


def get_state_from_text(text: str) -> Optional[str]:
    """Detects the state from document text."""
    text_lower = text.lower()
    if "uttar pradesh" in text_lower or "up police" in text_lower:
        return "UTTAR PRADESH"
    if "bihar" in text_lower or "bihar police" in text_lower:
        return "BIHAR"
    if "maharashtra" in text_lower:
        return "MAHARASHTRA"
    if "madhya pradesh" in text_lower or "mp police" in text_lower:
        return "MADHYA PRADESH"
    return None


def route_pvr_parser(text: str) -> Dict[str, Any]:
    """Routes document text to appropriate state parser from registry."""
    state = get_state_from_text(text)
    parser = get_pvr_parser(state)
    parsed_fields = parser.parse(text)
    if state is None:
        parsed_fields["review_required"] = True
    return parsed_fields
