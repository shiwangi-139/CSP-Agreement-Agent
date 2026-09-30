"""
app/expiry_engine.py
Dedicated deterministic compliance and expiry calculation engine.
Enforces strict regulatory and organizational business rules:
1. CSP Agreement:
   - If explicit 3-year validity is stated OR explicit expiry date printed:
     expiry = issue_date + 3 years (or printed expiry).
   - If agreement does NOT explicitly state 3-year validity:
     expiry = issue_date + AGREEMENT_DEFAULT_YEARS (2) years.
2. Police Verification / Character Certificate:
   - Exactly 1-year validity from issue date (or printed expiry if specified).
3. IIBF Certificate:
   - Lifetime validity (NO expiry date assigned).
Zero AI guesswork -- 100% deterministic, auditable business logic.
"""

from datetime import date, timedelta
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


# An agreement that states no validity (no 3-year line, no "valid from X to Y")
# is valid for this many years from its issue date.
AGREEMENT_DEFAULT_YEARS = 2


def add_years(start_dt: date, years: int) -> date:
    """Adds integer years handling leap year February 29th safely."""
    try:
        return start_dt.replace(year=start_dt.year + years)
    except ValueError:
        # Leap day adjustment: Feb 29 -> Feb 28
        return start_dt.replace(year=start_dt.year + years, day=28)


def add_months(start_dt: date, months: int) -> date:
    """Adds months, clamping to the last day of the target month."""
    from .ai.extraction.rules.dates import add_months as _add_months
    return _add_months(start_dt, months)


class ExpiryRule:
    AGREEMENT_EXPLICIT_3_YEAR = "AGREEMENT_EXPLICIT_3_YEAR"
    AGREEMENT_RANGE_FROM_TO = "AGREEMENT_RANGE_FROM_TO"
    PVR_STATED_6_MONTHS = "PVR_STATED_6_MONTHS"
    PVR_STATED_1_YEAR = "PVR_STATED_1_YEAR"
    AGREEMENT_DEFAULT_1_YEAR = "AGREEMENT_DEFAULT_1_YEAR"   # old rule, kept so older records still display
    AGREEMENT_DEFAULT_2_YEAR = "AGREEMENT_DEFAULT_2_YEAR"
    AGREEMENT_PRINTED_EXPIRY = "AGREEMENT_PRINTED_EXPIRY"
    PVR_DEFAULT_1_YEAR = "PVR_DEFAULT_1_YEAR"
    PVR_PRINTED_EXPIRY = "PVR_PRINTED_EXPIRY"
    PVR_PCC_LIFETIME = "PVR_PCC_LIFETIME"       # Police Clearance Certificate, no validity stated
    IIBF_LIFETIME_NO_EXPIRY = "IIBF_LIFETIME_NO_EXPIRY"
    MISSING_DATES = "MISSING_DATES"
    UNKNOWN_DOCUMENT = "UNKNOWN_DOCUMENT"


def calculate_document_expiry(
    doc_type: str,
    issue_date: Optional[date],
    explicit_expiry_date: Optional[date] = None,
    has_explicit_3year_clause: bool = False,
    today: Optional[date] = None,
    validity_months: Optional[int] = None,
    validity_rule: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Computes exact expiry date, validity rule, and compliance status for a document.

    Returns dict with keys:
    - status: 'VALID', 'EXPIRED', 'NEEDS_REVIEW', 'UNKNOWN'
    - calculated_expiry: date object or None
    - explicit_expiry: date object or None
    - validity_rule_used: string from ExpiryRule
    - reason: human-readable justification for audits
    """
    if today is None:
        today = date.today()

    norm_type = (doc_type or "UNKNOWN").upper().strip()

    # 1. CSP AGREEMENT
    if norm_type in ("AGREEMENT", "CSP_AGREEMENT"):
        if explicit_expiry_date:
            is_valid = explicit_expiry_date >= today
            return {
                "status": "VALID" if is_valid else "EXPIRED",
                "calculated_expiry": explicit_expiry_date,
                "explicit_expiry": explicit_expiry_date,
                "validity_rule_used": validity_rule or ExpiryRule.AGREEMENT_PRINTED_EXPIRY,
                "reason": f"Explicit expiry date {explicit_expiry_date.isoformat()} found printed in document."
            }

        if not issue_date:
            return {
                "status": "NEEDS_REVIEW",
                "calculated_expiry": None,
                "explicit_expiry": None,
                "validity_rule_used": ExpiryRule.MISSING_DATES,
                "reason": "Agreement issue date is missing or unreadable; manual review required."
            }

        if has_explicit_3year_clause:
            calc_exp = add_years(issue_date, 3)
            is_valid = calc_exp >= today
            return {
                "status": "VALID" if is_valid else "EXPIRED",
                "calculated_expiry": calc_exp,
                "explicit_expiry": None,
                "validity_rule_used": ExpiryRule.AGREEMENT_EXPLICIT_3_YEAR,
                "reason": f"Explicit 3-year validity clause verified. Expiry set to issue ({issue_date.isoformat()}) + 3 years."
            }
        else:
            # Business rule: validity not stated anywhere -> default of AGREEMENT_DEFAULT_YEARS.
            calc_exp = add_years(issue_date, AGREEMENT_DEFAULT_YEARS)
            is_valid = calc_exp >= today
            return {
                "status": "VALID" if is_valid else "EXPIRED",
                "calculated_expiry": calc_exp,
                "explicit_expiry": None,
                "validity_rule_used": ExpiryRule.AGREEMENT_DEFAULT_2_YEAR,
                "reason": f"No validity stated in the agreement. Applied the default {AGREEMENT_DEFAULT_YEARS}-year rule: issue ({issue_date.isoformat()}) + {AGREEMENT_DEFAULT_YEARS} years."
            }

    # 2. POLICE VERIFICATION / CHARACTER CERTIFICATE
    elif norm_type in ("POLICE_VERIFICATION", "CHARACTER_CERTIFICATE", "PVR"):
        if explicit_expiry_date:
            is_valid = explicit_expiry_date >= today
            return {
                "status": "VALID" if is_valid else "EXPIRED",
                "calculated_expiry": explicit_expiry_date,
                "explicit_expiry": explicit_expiry_date,
                "validity_rule_used": ExpiryRule.PVR_PRINTED_EXPIRY,
                "reason": f"Explicit police verification validity date {explicit_expiry_date.isoformat()} detected."
            }

        if not issue_date:
            return {
                "status": "NEEDS_REVIEW",
                "calculated_expiry": None,
                "explicit_expiry": None,
                "validity_rule_used": ExpiryRule.MISSING_DATES,
                "reason": "Police verification issue date is missing or unreadable; manual review required."
            }

        if validity_rule == ExpiryRule.PVR_PCC_LIFETIME:
            return {
                "status": "VALID",
                "calculated_expiry": None,
                "explicit_expiry": None,
                "validity_rule_used": ExpiryRule.PVR_PCC_LIFETIME,
                "reason": "Police Clearance Certificate with no validity stated: accepted for life."
            }

        months = validity_months or 12
        calc_exp = add_months(issue_date, months) if months != 12 else add_years(issue_date, 1)
        is_valid = calc_exp >= today
        rule = validity_rule or (ExpiryRule.PVR_STATED_6_MONTHS if months == 6 else ExpiryRule.PVR_DEFAULT_1_YEAR)
        return {
            "status": "VALID" if is_valid else "EXPIRED",
            "calculated_expiry": calc_exp,
            "explicit_expiry": None,
            "validity_rule_used": rule,
            "reason": f"{months}-month validity applied: issue ({issue_date.isoformat()}) + {months} months."
        }

    # 3. IIBF CERTIFICATE
    elif norm_type in ("IIBF_CERTIFICATE", "IIBF_CERTIFICATION", "IIBF"):
        return {
            "status": "VALID",
            "calculated_expiry": None,
            "explicit_expiry": None,
            "validity_rule_used": ExpiryRule.IIBF_LIFETIME_NO_EXPIRY,
            "reason": "IIBF Business Correspondent Certificate has lifetime validity (no expiry)."
        }

    # 4. UNKNOWN OR UNAUTHORIZED
    return {
        "status": "NEEDS_REVIEW",
        "calculated_expiry": None,
        "explicit_expiry": None,
        "validity_rule_used": ExpiryRule.UNKNOWN_DOCUMENT,
        "reason": f"Unrecognized document type: '{doc_type}'."
    }

