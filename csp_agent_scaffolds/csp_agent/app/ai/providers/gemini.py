"""
Uses the current google-genai SDK. The older google.generativeai package
is fully deprecated (end-of-life Nov 30, 2025 per Google's own repo) --
verified before writing this, not assumed from a remembered package name.
"""
import json
import re
from ...config import GEMINI_API_KEY, GEMINI_MODEL

# Gemini is no longer in the extraction chain (see app/ai/providers/vision_fallback.py).
# The SDK is imported only if this legacy provider is actually called, so the app
# starts without google-genai installed.
_client = None


def _sdk():
    global _client
    from google import genai
    from google.genai import types
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client, types

EXTRACTION_PROMPT = """You are an expert document analysis assistant assisting a human banking compliance reviewer.
Analyze the provided document (text and/or image). Return ONLY valid JSON, no markdown fences, no explanatory text outside the JSON.

CRITICAL READABILITY & BLURRINESS CHECK:
First inspect whether the document is legible:
- If the document image or text is too blurry, out-of-focus, low-resolution, cut off, unreadable, or illegible such that critical fields (like dates or CSP code) CANNOT be reliably identified:
  You MUST return this exact JSON:
  {{
    "document_type": "REJECTED_BLURRY",
    "is_visible": false,
    "is_blurry": true,
    "rejection_reason": "DOCUMENT_BLURRY_OR_ILLEGIBLE: Text or image is too blurry/low-quality to decipher reliably.",
    "overall_confidence": 0.0
  }}

If the document IS legible and readable, set "is_visible": true, "is_blurry": false, and classify into one of the four types:
1. "AGREEMENT" (Customer Service Point / CSP Agreement)
2. "POLICE_VERIFICATION" (Police verification / Character certificate)
3. "IIBF_CERTIFICATION" (Indian Institute of Banking & Finance certificate)
4. "UNKNOWN"

Field requirements by type:

If AGREEMENT, output JSON with this exact structure:
{{
  "document_type": "AGREEMENT",
  "is_visible": true,
  "is_blurry": false,
  "has_explicit_3year_clause": true or false,
  "csp_code": {{"value": "...", "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "csp_name": {{"value": "...", "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "agreement_start_date": {{"value": "YYYY-MM-DD" or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "agreement_expiry_date": {{"value": "YYYY-MM-DD" or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "signature": {{"detected": true or false, "confidence": 0.0-1.0, "page": 1, "notes": "..."}},
  "stamp": {{"detected": true or false, "confidence": 0.0-1.0, "page": 1, "notes": "..."}},
  "issues": [],
  "overall_confidence": 0.0-1.0
}}
IMPORTANT AGREEMENT VALIDITY RULES:
- Inspect the document for explicit validity term (e.g. "valid for a period of three (3) years" or "Agreement is valid for 3 years"):
  - If explicitly mentioned: set "has_explicit_3year_clause": true, and agreement_expiry_date = agreement_start_date + 3 years.
  - If NO explicit 3-year term is mentioned anywhere (older format): set "has_explicit_3year_clause": false, and agreement_expiry_date = agreement_start_date + 1 year (1 year default rule).
- Note on CSP code: Standard CSP codes consist of 1 digit + 1 uppercase letter + 6 digits (e.g. 1A852474).

If POLICE_VERIFICATION, output JSON with this exact structure:
{{
  "document_type": "POLICE_VERIFICATION",
  "is_visible": true,
  "is_blurry": false,
  "csp_code": {{"value": "..." or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "verification_date": {{"value": "YYYY-MM-DD" or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "expiry_date": {{"value": "YYYY-MM-DD" or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "issuing_authority": {{"value": "...", "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "issues": [],
  "overall_confidence": 0.0-1.0
}}
(Police verification and character certificates are strictly valid for 1 YEAR from issue date. Compute expiry_date as verification_date + 1 year).

If IIBF_CERTIFICATION, output JSON with this exact structure:
{{
  "document_type": "IIBF_CERTIFICATION",
  "is_visible": true,
  "is_blurry": false,
  "holder_name": {{"value": "...", "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "membership_number": {{"value": "..." or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "exam_name": {{"value": "...", "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "issue_date": {{"value": "YYYY-MM-DD" or null, "confidence": 0.0-1.0, "page": 1, "evidence": "..."}},
  "expiry_date": {{"value": "LIFETIME_NO_EXPIRY", "confidence": 1.0, "page": 1, "evidence": "IIBF certificates have lifetime validity"}},
  "issues": [],
  "overall_confidence": 0.0-1.0
}}

If UNKNOWN or none of the above, output:
{{
  "document_type": "UNKNOWN",
  "is_visible": true,
  "is_blurry": false,
  "reason": "..."
}}

Document text context:
{document_text}
"""



def extract_fields(
    document_text: str = "",
    expected_type: str | None = None,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> dict:
    prompt_text = EXTRACTION_PROMPT.format(document_text=(document_text or "")[:15000])

    contents = []
    if file_bytes and mime_type:
        contents.append(_sdk()[1].Part.from_bytes(data=file_bytes, mime_type=mime_type))
    contents.append(prompt_text)

    response = _sdk()[0].models.generate_content(model=GEMINI_MODEL, contents=contents)
    text = response.text.strip()

    # Extract JSON between the outermost brackets
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        clean_json = match.group(0)
    else:
        clean_json = text

    try:
        return json.loads(clean_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"Gemini returned invalid JSON: {e}\nRaw text: {text}") from e


def draft_message(template_name: str, context: dict) -> str:
    prompt = (
        f"Draft a short, professional {template_name} message using this context: "
        f"{context}. Plain text only, no markdown."
    )
    response = _sdk()[0].models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return response.text.strip()
