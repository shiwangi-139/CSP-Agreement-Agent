"""
app/ai/extraction/deterministic_extractor.py

One call reads one file and returns a FINAL decision:

  READABLE     dates found -> stored as the CSP's current document
  UNREADABLE   blurry / dates unreadable after every step -> kept for audit
               only; the document type counts as MISSING and the CSP gets an
               upload link asking for a clear scanned PDF (never the review
               queue)
  NOT_ALLOWED  not one of the three documents (Aadhaar, PAN, ...), or an
               unsupported file

Steps (all free):
  1. scan_document: text layer for digital pages, Tesseract English (plus
     Hindi when the page isn't clearly English) with OpenCV clean-up for
     scanned pages and photos; blur + OCR confidence per page; cached by
     the file's SHA-256.
  2. allowlist gate: AGREEMENT / POLICE_VERIFICATION / IIBF_CERTIFICATE.
  3. rules (app/ai/extraction/rules): issue date, validity, IIBF name/number.
  4. only if a required field is still missing: the local vision model
     (then Groq) reads the most relevant page image and may fill ONLY the
     missing fields. It never sets is_allowed or the CSP code.
"""
import io
import logging
import re
from datetime import date
from typing import Any, Dict, Optional

from ...config import CSP_CODE_REGEX
from ...expiry_engine import calculate_document_expiry, add_years, ExpiryRule, AGREEMENT_DEFAULT_YEARS  # noqa: F401 (add_years re-exported)
from ...ocr_service import scan_document, classify_allowlist_gate, DocumentScan
from ...validation import normalize_csp_code
from .rules import extract_agreement, extract_pvr, extract_iibf
from .rules.agreement import HEADING, ESTAMP_LABEL
from .rules.dates import find_dates, plausible_issue_date

logger = logging.getLogger(__name__)

CSP_CODE_PATTERN = re.compile(CSP_CODE_REGEX)
ALLOWED_TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")

READABLE, UNREADABLE, NOT_ALLOWED = "READABLE", "UNREADABLE", "NOT_ALLOWED"
AGREEMENT_STRUCTURE = re.compile(r"on\s*this\s*day\s*of|effective\s*date|e-?\s*stamp|non[\s-]*judicial|"
                                 r"issu\w*\s*date|hereinafter|witness\s*whereof|initials\s*of", re.I)


def _rules_for(doc_type: str, text: str, today: date) -> dict:
    if doc_type == "AGREEMENT":
        return extract_agreement(text, today)
    if doc_type == "POLICE_VERIFICATION":
        return extract_pvr(text, today)
    if doc_type == "IIBF_CERTIFICATE":
        return extract_iibf(text, today)
    return {}


def _enough(text: str, today: date) -> bool:
    """Early stop for long PDFs: the fields we need are already found."""
    ok, doc_type, _, _ = classify_allowlist_gate(text)
    if not ok:
        return False
    r = _rules_for(doc_type, text, today)
    if doc_type == "AGREEMENT":
        # Validity is settled by a "valid for 3 years" clause or a printed
        # "from ... to ..." range; without either, keep reading the body.
        return bool(r.get("issue_date")) and bool(
            r.get("has_explicit_3year_clause") or r.get("validity_source") == "AGREEMENT_RANGE_FROM_TO")
    if doc_type == "POLICE_VERIFICATION":
        return r.get("date_source") == "PVR_DIGITAL_SIGNATURE_DATE"
    if doc_type == "IIBF_CERTIFICATE":
        return r.get("date_source") == "IIBF_DATED" and bool(r.get("holder_name"))
    return False


OWNER_FIELDS = re.compile(r"CSP\s*(?:Name|Code)|hereby\s*appoints|certif\w*\s*that|purchased\s*by", re.I)


def _page_for_model(scan: DocumentScan, doc_type: str, owner: bool = False) -> Optional[bytes]:
    """The page most likely to hold the issue date (or, with owner=True, the
    CSP code and name), as a JPEG no larger than ~1600 px (small enough for a
    local 7B vision model)."""
    if not scan.pages:
        return None
    best = scan.pages[0]
    if owner:
        for p in scan.pages:
            if re.search(r"CSP\s*Code|hereby\s*appoints", p.text, re.I):
                best = p
                break
        else:
            best = next((p for p in scan.pages if OWNER_FIELDS.search(p.text)), best)
    elif doc_type == "AGREEMENT":
        for p in scan.pages:
            if ESTAMP_LABEL.search(p.text) or HEADING.search(p.text):
                best = p
                break
    png = best.image_png or scan.page_image(best.index)
    if not png:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("RGB")
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"page image preparation failed: {e}")
        return None


def _ask_model(scan: DocumentScan, doc_type: str) -> Optional[dict]:
    image = _page_for_model(scan, doc_type)
    if image is None:
        return None
    from ..providers.vision_fallback import read_page
    return read_page(image, doc_type, "image/jpeg")


def read_owner_with_model(file_bytes: bytes, doc_type: str) -> Optional[dict]:
    """Ask the vision model (local Ollama, then Groq) for the CSP code and
    name on the page that holds them. For documents whose owner OCR could
    not confirm, typically handwritten. Returns {"csp_code", "csp_name",
    "provider"} or None."""
    scan = scan_document(file_bytes)
    image = _page_for_model(scan, doc_type, owner=True)
    if image is None:
        return None
    from ..providers.vision_fallback import read_page
    ans = read_page(image, doc_type, "image/jpeg")
    if not ans:
        return None
    return {"csp_code": ans.get("csp_code"), "csp_name": ans.get("csp_name"), "provider": ans.get("provider")}


def _model_date(ans: dict, today: date) -> Optional[date]:
    raw = ans.get("issue_date")
    if not raw:
        return None
    try:
        d = date.fromisoformat(str(raw)[:10])
    except ValueError:
        found = find_dates(str(raw))
        d = found[0].value if found else None
    return d if d and plausible_issue_date(d, today) else None


def _result(decision: str, **kw) -> Dict[str, Any]:
    base = {
        "readability": decision,
        "readability_reason": None,
        "is_allowed": decision == READABLE,
        "is_visible": decision != UNREADABLE,
        "is_blurry": decision == UNREADABLE,
        "document_type": "UNKNOWN",
        "rejection_reason": None,
        "csp_code": None,
        "start_date": None,
        "expiry_date": None,
        "explicit_expiry": None,
        "has_explicit_3year_clause": False,
        "validity_months": None,
        "validity_rule_used": None,
        "date_source": None,
        "compliance_status": None,
        "iibf_registration_number": None,
        "holder_name": None,
        "confidence": 0.0,
        "field_confidences": {},
        "extraction_method": None,
        "ocr_method": None,
        "page_count": 0,
        "pages_readable": 0,
        "model_provider": None,
        "text_chars": 0,
    }
    base.update(kw)
    return base


def extract_document_fields_deterministic(
    pdf_bytes: bytes,
    filename: str = "",
    allow_llm_fallback: bool = True,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Read one file (PDF, JPEG or PNG) and return the final decision plus
    every extracted field. The name is historical; it now handles photos."""
    today = today or date.today()
    scan = scan_document(pdf_bytes, stop_when=lambda t: _enough(t, today))
    text = scan.text
    # Every CSP code and the text itself, so the caller can check whose
    # document this is (app/gmail_ingest.py: check_owner). Not stored.
    codes = list(dict.fromkeys(normalize_csp_code(c) for c in CSP_CODE_PATTERN.findall(text.upper())))
    common = dict(ocr_method=scan.method, page_count=scan.page_count,
                  pages_readable=scan.readable_pages, text_chars=len(text),
                  csp_codes=codes, match_text=text[:20000])

    if scan.mime_type is None:
        return _result(NOT_ALLOWED, rejection_reason="Unsupported file type (only PDF, JPG, PNG).",
                       extraction_method="FILE_TYPE_REJECTED", **common)
    if not scan.pages:
        return _result(UNREADABLE, readability_reason="File could not be opened.",
                       rejection_reason="File could not be opened.", extraction_method="OPEN_FAILED", **common)

    readable, why = scan.readability()
    is_allowed, doc_type, gate_reason, gate_conf = classify_allowlist_gate(text, filename)
    if doc_type == "REJECTED_UNAUTHORIZED":
        return _result(NOT_ALLOWED, document_type=doc_type, rejection_reason=gate_reason,
                       confidence=gate_conf, extraction_method="ALLOWLIST_GATE_REJECTED", **common)

    model_ans = None
    if doc_type not in ALLOWED_TYPES:
        # Unreadable text can hide an allowed document (a blurry photo of an
        # agreement). Only then ask the model what it is.
        if readable:
            return _result(NOT_ALLOWED, document_type=doc_type, rejection_reason=gate_reason,
                           extraction_method="ALLOWLIST_GATE_UNKNOWN", **common)
        if allow_llm_fallback:
            model_ans = _ask_model(scan, "UNKNOWN")
        if model_ans and model_ans.get("is_legible") and model_ans.get("document_type") in ALLOWED_TYPES:
            doc_type, gate_conf = model_ans["document_type"], 0.75
        else:
            reason = why if not readable else "Document type could not be identified."
            return _result(UNREADABLE, readability_reason=reason, rejection_reason=reason,
                           extraction_method="UNREADABLE_UNKNOWN_TYPE",
                           model_provider=(model_ans or {}).get("provider"), **common)

    r = _rules_for(doc_type, text, today)
    issue = r.get("issue_date")
    date_source = r.get("date_source")
    three_year = bool(r.get("has_explicit_3year_clause"))
    months = r.get("validity_months")
    validity_source = r.get("validity_source")
    holder = r.get("holder_name")
    method = "RULES_" + scan.method

    low_quality_ocr = any(p.source == "OCR" and (p.ocr_confidence or 0) < 70 for p in scan.pages)
    if issue is None and allow_llm_fallback:
        if model_ans is None:
            model_ans = _ask_model(scan, doc_type)
        if model_ans and model_ans.get("is_legible"):
            d = _model_date(model_ans, today)
            if d:
                issue, date_source = d, "MODEL_VISION_" + doc_type
                method = "RULES_PLUS_LOCAL_MODEL"
                # The model may upgrade validity only where OCR was too poor
                # for the rules to see the clause.
                if doc_type == "AGREEMENT" and low_quality_ocr and model_ans.get("three_year_validity_stated") and not three_year:
                    three_year, validity_source = True, "MODEL_VISION_3_YEAR_CLAUSE"
                if doc_type == "POLICE_VERIFICATION" and model_ans.get("validity_months_stated") in (6, 12) \
                        and validity_source == "PVR_DEFAULT_1_YEAR":
                    months = model_ans["validity_months_stated"]
                    validity_source = f"MODEL_VISION_{months}_MONTHS"
                if doc_type == "IIBF_CERTIFICATE" and not holder and model_ans.get("holder_name"):
                    holder = str(model_ans["holder_name"]).strip().title()[:80]

    code_match = CSP_CODE_PATTERN.search(text.upper())
    csp_code = normalize_csp_code(code_match.group(0)) if code_match else None

    if issue is None and readable and doc_type == "AGREEMENT" and not AGREEMENT_STRUCTURE.search(text):
        # Readable, mentions the agreement, but has none of its structure:
        # a questionnaire, BCP or letter that refers to the agreement.
        return _result(NOT_ALLOWED, document_type="UNKNOWN", csp_code=csp_code,
                       rejection_reason="Mentions the CSP agreement but is not the agreement itself.",
                       extraction_method="NOT_THE_AGREEMENT", **common)
    if issue is None:
        reason = why if not readable else "Issue date could not be read (blank or unclear date field)."
        return _result(UNREADABLE, document_type=doc_type, csp_code=csp_code,
                       readability_reason=reason, rejection_reason=reason,
                       extraction_method="UNREADABLE_NO_ISSUE_DATE",
                       model_provider=(model_ans or {}).get("provider"), **common)

    rule_for_engine = None
    if validity_source in ("AGREEMENT_RANGE_FROM_TO",):
        rule_for_engine = ExpiryRule.AGREEMENT_RANGE_FROM_TO
    elif validity_source and validity_source.startswith(("PVR_STATED", "PVR_PCC", "MODEL_VISION_6", "MODEL_VISION_12")):
        rule_for_engine = validity_source
    exp = calculate_document_expiry(
        doc_type=doc_type, issue_date=issue, explicit_expiry_date=r.get("explicit_expiry"),
        has_explicit_3year_clause=three_year, today=today,
        validity_months=months, validity_rule=rule_for_engine,
    )
    expiry = exp["calculated_expiry"]

    from_model = date_source.startswith("MODEL_VISION")
    field_conf = {
        "document_type": gate_conf,
        "issue_date": 0.80 if from_model else 0.95,
        "validity": 0.80 if (validity_source or "").startswith("MODEL_VISION") else 0.95,
    }
    confidence = round(min(field_conf.values()), 2)

    return _result(
        READABLE,
        readability_reason=why,
        document_type=doc_type,
        csp_code=csp_code,
        start_date=issue.isoformat(),
        expiry_date=expiry.isoformat() if expiry else (
            "LIFETIME_NO_EXPIRY" if doc_type == "IIBF_CERTIFICATE" or validity_source == "PVR_PCC_LIFETIME" else None),
        explicit_expiry=r.get("explicit_expiry").isoformat() if r.get("explicit_expiry") else None,
        has_explicit_3year_clause=three_year,
        validity_months=months if doc_type == "POLICE_VERIFICATION" else (36 if three_year else (12 * AGREEMENT_DEFAULT_YEARS if doc_type == "AGREEMENT" and not r.get("explicit_expiry") else None)),
        validity_rule_used=exp["validity_rule_used"],
        date_source=date_source,
        compliance_status=exp["status"],
        iibf_registration_number=r.get("registration_number"),
        holder_name=holder,
        model_csp_code=(model_ans or {}).get("csp_code"),
        model_csp_name=(model_ans or {}).get("csp_name"),
        confidence=confidence,
        field_confidences=field_conf,
        extraction_method=method,
        model_provider=(model_ans or {}).get("provider"),
        **common,
    )
