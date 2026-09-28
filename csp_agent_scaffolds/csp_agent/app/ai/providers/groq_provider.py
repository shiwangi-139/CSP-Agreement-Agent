"""
app/ai/providers/groq_provider.py
High-speed, free LLM provider using Groq / OpenAI-compatible API:
- Supports models: llama-3.3-70b-versatile, llama-3.1-8b-instant, mixtral-8x7b-32768.
- 100% Free tier (up to 30 requests/minute, thousands per day).
- Returns structured JSON for compliance verification.
"""

import json
import logging
import os
import re
import urllib.request
from typing import Union

from ..schemas import (
    AgreementExtraction,
    PoliceVerificationExtraction,
    IIBFCertificationExtraction,
    UnknownDocumentExtraction,
)
from .gemini import EXTRACTION_PROMPT

logger = logging.getLogger(__name__)

# Groq settings
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# NVIDIA NIM (Nemotron) settings
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")
NVIDIA_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"


import base64

# Vision model settings
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")


def validate_document_groq_text(
    document_text: str
) -> dict:
    """
    Tier 1 (Option A): Analyzes document text using Groq LLaMA 3.3 70B or NVIDIA Nemotron.
    Returns parsed dictionary.
    """
    ai_provider = os.getenv("AI_PROVIDER", "").lower()
    if ai_provider == "nvidia" or (NVIDIA_API_KEY and not GROQ_API_KEY):
        api_key = NVIDIA_API_KEY
        model = NVIDIA_MODEL
        endpoint = NVIDIA_ENDPOINT
    else:
        api_key = GROQ_API_KEY
        model = GROQ_MODEL
        endpoint = os.getenv("OPENAI_COMPATIBLE_ENDPOINT", GROQ_ENDPOINT)

    if not api_key:
        raise ValueError("Neither GROQ_API_KEY nor NVIDIA_API_KEY is configured in .env")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "CSP-Compliance-Agent/2.0"
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": f"Analyze this CSP compliance document:\n\n{document_text[:12000]}"}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 1500
    }

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        raw_text = data["choices"][0]["message"]["content"]
        return json.loads(raw_text)


def validate_document_groq_vision(
    image_bytes: bytes,
    mime_type: str = "image/jpeg"
) -> dict:
    """
    Tier 2 (Option B): Cloud Vision API Fallback.
    Directly inspects the rendered document image using Groq Vision or Gemini Flash Vision.
    Detects if the document is blurry, illegible, or extracts dates visually.
    """
    # Attempt Groq Vision first if GROQ_API_KEY is available
    if GROQ_API_KEY:
        try:
            b64_img = base64.b64encode(image_bytes).decode("utf-8")
            headers = {
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "CSP-Compliance-Agent/2.0"
            }
            payload = {
                "model": GROQ_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": EXTRACTION_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{b64_img}"
                                }
                            }
                        ]
                    }
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1500
            }
            req = urllib.request.Request(
                GROQ_ENDPOINT,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw_text = data["choices"][0]["message"]["content"]
                return json.loads(raw_text)
        except Exception as ge:
            logger.warning(f"Groq Vision call failed ({ge}), attempting Gemini Vision fallback...")

    # Fallback to Gemini Flash Vision
    from .gemini import extract_fields as gemini_extract_fields
    return gemini_extract_fields(file_bytes=image_bytes, mime_type=mime_type)


def validate_document_groq(
    document_text: str
) -> Union[AgreementExtraction, PoliceVerificationExtraction, IIBFCertificationExtraction, UnknownDocumentExtraction]:
    """Analyzes document text and returns typed Pydantic schema."""
    parsed = validate_document_groq_text(document_text)
    doc_type = parsed.get("document_type")
    if doc_type == "AGREEMENT":
        return AgreementExtraction.model_validate(parsed)
    elif doc_type == "POLICE_VERIFICATION":
        return PoliceVerificationExtraction.model_validate(parsed)
    elif doc_type == "IIBF_CERTIFICATION":
        return IIBFCertificationExtraction.model_validate(parsed)
    else:
        return UnknownDocumentExtraction.model_validate(parsed)


def extract_fields(
    document_text: str = "",
    expected_type: str | None = None,
    file_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> dict:
    """Standardized extract_fields interface for AI service."""
    if file_bytes:
        return validate_document_groq_vision(file_bytes, mime_type or "image/jpeg")
    return validate_document_groq_text(document_text)


def draft_message(template_name: str, context: dict) -> str:
    """Drafts compliant notification message for CSP renewal."""
    return f"Reminder for template {template_name}: {context}"

