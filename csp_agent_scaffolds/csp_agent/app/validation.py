"""
Deterministic rules that run AFTER the AI extraction and are never
overridden by it. This is the actual safety layer for a compliance system --
the model proposes, this module decides.

Important: "AUTO_ACCEPT_CANDIDATE" is an internal technical result meaning
"passed automated checks, ready for a human to approve" -- it is NOT
authorization to renew an agreement. See app/api/review.py's
approve-renewal endpoint for the only place that's allowed to happen.
"""
import logging
from .config import AI_AUTO_REVIEW_THRESHOLD, AI_HUMAN_REVIEW_THRESHOLD
from .ai.schemas import (
    AgreementExtraction, PoliceVerificationExtraction, BCFCertificationExtraction, UnknownDocument,
)

logger = logging.getLogger(__name__)


def normalize_csp_code(code: str | None) -> str:
    """Normalize CSP codes and resolve common OCR character confusions.
    Standard CSP code format is 1 digit + 1 uppercase letter + 6 digits (e.g. 1A852474).
    """
    if not code:
        return ""
    code = code.strip().upper()
    code = "".join(ch for ch in code if ch.isalnum())
    if len(code) == 8 and code[0].isdigit() and code[1].isalpha():
        chars = list(code)
        replacements = {"R": "8", "B": "8", "O": "0", "D": "0", "I": "1", "L": "1", "S": "5", "Z": "2"}
        for i in range(2, 8):
            if chars[i] in replacements:
                chars[i] = replacements[chars[i]]
        return "".join(chars)
    return code


def apply_deterministic_checks(extraction, known_csp_codes: set[str]) -> dict:
    if isinstance(extraction, UnknownDocument):
        return {
            "decision": "REJECT_OR_MANUAL",
            "issues": [getattr(extraction, "reason", "unrecognized_document")],
            "visual_review_required": True,
        }

    issues = list(getattr(extraction, "issues", []))

    if isinstance(extraction, AgreementExtraction):
        start = extraction.agreement_start_date.value if extraction.agreement_start_date else None
        expiry = extraction.agreement_expiry_date.value if extraction.agreement_expiry_date else None
        if start and expiry and start >= expiry:
            issues.append("start_date_not_before_expiry")

        code_val = extraction.csp_code.value if extraction.csp_code else None
        norm_code = normalize_csp_code(code_val)
        if norm_code in known_csp_codes:
            extraction.csp_code.value = norm_code
        elif code_val and known_csp_codes and code_val not in known_csp_codes:
            issues.append("csp_code_not_recognized")

        if not extraction.signature or not extraction.signature.detected:
            issues.append("signature_not_detected")
        if not extraction.stamp or not extraction.stamp.detected:
            issues.append("stamp_not_detected")

    elif isinstance(extraction, PoliceVerificationExtraction):
        v_date = extraction.verification_date.value if extraction.verification_date else None
        exp_date = extraction.expiry_date.value if extraction.expiry_date else None
        if v_date and exp_date and v_date >= exp_date:
            issues.append("verification_date_not_before_expiry")

        code_val = extraction.csp_code.value if extraction.csp_code else None
        if code_val and known_csp_codes and code_val not in known_csp_codes:
            issues.append("csp_code_not_recognized")

    elif isinstance(extraction, BCFCertificationExtraction):
        if not extraction.holder_name or not extraction.holder_name.value:
            issues.append("missing_holder_name")

    confidence = getattr(extraction, "overall_confidence", 0.0)
    if confidence >= AI_AUTO_REVIEW_THRESHOLD and not issues:
        decision = "AUTO_ACCEPT_CANDIDATE"
    elif confidence >= AI_HUMAN_REVIEW_THRESHOLD:
        decision = "HUMAN_REVIEW"
    else:
        decision = "REJECT_OR_MANUAL"

    if issues and decision == "AUTO_ACCEPT_CANDIDATE":
        decision = "HUMAN_REVIEW"

    result = {
        "decision": decision,
        "issues": issues,
        "visual_review_required": True,
    }

    logger.info(
        "extraction_decision",
        extra={
            "decision": decision,
            "reason": ",".join(issues) if issues else "none",
        },
    )
    return result
