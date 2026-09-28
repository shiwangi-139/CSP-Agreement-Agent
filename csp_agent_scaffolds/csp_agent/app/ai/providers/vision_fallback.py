"""
Free vision fallback for pages the OCR rules could not read.

Order: local Ollama model on the rack server (LOCAL_VLM_URL / LOCAL_VLM_MODEL,
e.g. qwen2.5vl:7b -- open weights, reads Hindi, no per-call cost, documents
never leave the company), then Groq's free tier only if Ollama is down.

The model is asked a narrow question about ONE page image and may only fill
fields the rules left empty. It never decides whether a document is
allowed or which CSP it belongs to; the caller re-checks everything.
"""
import base64
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Optional

from ...config import (
    LOCAL_VLM_URL, LOCAL_VLM_MODEL, LOCAL_VLM_TIMEOUT_SECONDS,
    GROQ_API_KEY, GROQ_VISION_MODEL,
)

logger = logging.getLogger(__name__)

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS_ENDPOINT = "https://api.groq.com/openai/v1/models"
# Total seconds one page may spend waiting on Groq rate limits.
GROQ_MAX_RATE_LIMIT_WAIT = 20
# Tried in order after the configured model. Groq retires models often; a
# model the account can't use is skipped for the rest of the run.
GROQ_VISION_FALLBACKS = ["qwen/qwen3.8-27b"]
_groq_bad_models: set[str] = set()
_ollama_down_until = 0.0

DOC_NAMES = {
    "AGREEMENT": "Customer Service Point (CSP) Agreement or its e-stamp paper",
    "POLICE_VERIFICATION": "Police Verification / Character Certificate",
    "IIBF_CERTIFICATE": "IIBF BC/BF examination certificate",
    "UNKNOWN": "document that may be a CSP Agreement, Police Verification/Character Certificate, or IIBF certificate",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "is_legible": {"type": "boolean"},
        "document_type": {"type": "string", "enum": ["AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE", "OTHER"]},
        "issue_date": {"type": ["string", "null"]},
        "issue_date_evidence": {"type": ["string", "null"]},
        "three_year_validity_stated": {"type": ["boolean", "null"]},
        "validity_months_stated": {"type": ["integer", "null"]},
        "holder_name": {"type": ["string", "null"]},
    },
    "required": ["is_legible", "document_type", "issue_date"],
}


def _prompt(doc_type: str) -> str:
    return (
        f"This image is one page of a {DOC_NAMES.get(doc_type, DOC_NAMES['UNKNOWN'])} from India. "
        "Text may be English or Hindi, printed or handwritten.\n"
        "Answer ONLY with JSON matching this schema: " + json.dumps(SCHEMA["properties"]) + "\n"
        "Rules:\n"
        "- is_legible=false if the page is too blurry, dark, cut off or small to read dates reliably.\n"
        "- issue_date: YYYY-MM-DD, or null if you cannot read it with certainty. Never guess.\n"
        "  AGREEMENT: the e-stamp 'Certificate Issued Date', else the date under the heading "
        "'CUSTOMER SERVICE POINT AGREEMENT' (\"On this day of ...\").\n"
        "  POLICE_VERIFICATION: the digital-signature date if present, else the date at the top.\n"
        "  IIBF_CERTIFICATE: the 'DATED' date near the signature.\n"
        "- issue_date_evidence: copy the exact words around the date.\n"
        "- three_year_validity_stated: true only if the page says the agreement is valid for three (3) years.\n"
        "- validity_months_stated: 6 or 12 only if the page states it (e.g. 'valid for six months').\n"
    )


def _parse_json(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        raise ValueError("model returned no JSON")
    return json.loads(m.group(0))


def _post(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _ollama_reachable() -> bool:
    """A 2-second TCP check, so a missing Ollama costs 2 s, not the full timeout."""
    from urllib.parse import urlparse
    u = urlparse(LOCAL_VLM_URL)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 11434), timeout=2):
            return True
    except OSError:
        return False


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode(errors="replace")[:300]
    except Exception:
        return ""


def _ask_ollama(image: bytes, doc_type: str) -> dict:
    payload = {
        "model": LOCAL_VLM_MODEL,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [{"role": "user", "content": _prompt(doc_type),
                      "images": [base64.b64encode(image).decode()]}],
    }
    data = _post(f"{LOCAL_VLM_URL}/api/chat", payload, {"Content-Type": "application/json"},
                 LOCAL_VLM_TIMEOUT_SECONDS)
    return _parse_json(data["message"]["content"])


def _ask_groq(image: bytes, doc_type: str, mime: str, model: str) -> dict:
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "max_tokens": 600,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _prompt(doc_type)},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}},
        ]}],
    }
    data = _post(GROQ_ENDPOINT, payload,
                 {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json",
                  "User-Agent": "CSP-Compliance-Agent/3.0"}, 60)
    return _parse_json(data["choices"][0]["message"]["content"])


def _ask_groq_with_backoff(image: bytes, doc_type: str, mime: str, model: str, attempts: int = 5) -> dict:
    """Groq's free tier allows only so many requests per minute. On 429 wait
    as long as Groq asks (Retry-After) and try again, so a busy minute never
    turns a readable document into "unreadable"."""
    waited = 0.0
    for attempt in range(attempts):
        try:
            return _ask_groq(image, doc_type, mime, model)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == attempts - 1 or waited >= GROQ_MAX_RATE_LIMIT_WAIT:
                raise
            try:
                wait = float(e.headers.get("retry-after") or 0)
            except (TypeError, ValueError):
                wait = 0
            wait = min(max(wait, 5 * (attempt + 1)), GROQ_MAX_RATE_LIMIT_WAIT - waited)
            logger.info("groq_rate_limited model=%s waiting=%.0fs (attempt %d)", model, wait, attempt + 1)
            time.sleep(wait)
            waited += wait


def read_page(image: bytes, doc_type: str, mime: str = "image/png") -> Optional[dict]:
    """Ask the local model, then Groq. Returns the parsed answer plus
    `provider`, or None if no model is reachable."""
    global _ollama_down_until
    if LOCAL_VLM_URL and time.time() >= _ollama_down_until and not _ollama_reachable():
        _ollama_down_until = time.time() + 300
        logger.warning("local_vlm_unreachable url=%s (skipping it for 5 min)", LOCAL_VLM_URL)
    if LOCAL_VLM_URL and time.time() >= _ollama_down_until:
        try:
            ans = _ask_ollama(image, doc_type)
            ans["provider"] = f"OLLAMA:{LOCAL_VLM_MODEL}"
            return ans
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            # Don't wait on a dead server for every page of every document.
            _ollama_down_until = time.time() + 300
            logger.warning("local_vlm_unreachable url=%s err=%s (skipping it for 5 min)", LOCAL_VLM_URL, e)
        except Exception as e:
            logger.warning("local_vlm_bad_answer err=%s", e)
    if GROQ_API_KEY:
        for model in dict.fromkeys([m for m in [GROQ_VISION_MODEL, *GROQ_VISION_FALLBACKS] if m]):
            if model in _groq_bad_models:
                continue
            try:
                ans = _ask_groq_with_backoff(image, doc_type, mime, model)
                ans["provider"] = f"GROQ:{model}"
                return ans
            except urllib.error.HTTPError as e:
                body = _http_error_text(e)
                if e.code == 404 or (e.code == 400 and "model" in body.lower()):
                    # Model retired or not available to this account: stop asking for it.
                    _groq_bad_models.add(model)
                    logger.warning("groq_vision_model_unavailable model=%s http=%s body=%s, trying next",
                                   model, e.code, body)
                    continue
                logger.warning("groq_vision_failed model=%s http=%s body=%s", model, e.code, body)
                break
            except Exception as e:
                logger.warning("groq_vision_failed model=%s err=%s", model, e)
                break
    return None


def check_groq_vision_model() -> Optional[str]:
    """Called once at worker start: warn clearly if GROQ_VISION_MODEL is not
    a model this Groq account can use. Returns the warning, or None."""
    if not GROQ_API_KEY or not GROQ_VISION_MODEL:
        return None
    try:
        req = urllib.request.Request(GROQ_MODELS_ENDPOINT, headers={
            "Authorization": f"Bearer {GROQ_API_KEY}", "User-Agent": "CSP-Compliance-Agent/3.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ids = {m.get("id") for m in json.loads(resp.read().decode()).get("data", [])}
    except Exception as e:
        logger.info("groq_model_check_skipped err=%s", e)
        return None
    if GROQ_VISION_MODEL in ids:
        return None
    vision_like = sorted(i for i in ids if i and re.search(r"vision|scout|maverick|vl|llava", i, re.I))
    msg = (f"GROQ_VISION_MODEL={GROQ_VISION_MODEL} is not available to this Groq account. "
           f"Models that may read images: {', '.join(vision_like) or 'none listed'}.")
    logger.warning("groq_vision_model_missing %s", msg)
    return msg
