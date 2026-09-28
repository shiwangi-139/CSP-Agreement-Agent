"""
app/comms/whatsapp.py
Adapter for the company's WhatsApp agent (send-only: it cannot read replies,
so "no response" always means "no new valid upload").

WHATSAPP_MODE:
  stub  (default, testing) - nothing leaves the server; an approved message
        is marked READY_NOT_SENT so the dashboard shows exactly what would go.
  push  - POST each message to WHATSAPP_AGENT_URL with WHATSAPP_AGENT_TOKEN.
  pull  - the WhatsApp agent polls GET /api/agent/outbox and acknowledges each
        message with POST /api/agent/outbox/{id}/ack.

The destination has already passed the recipient guard in outbound.py
before anything here runs.
"""
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from ..config import WHATSAPP_MODE, WHATSAPP_AGENT_URL, WHATSAPP_AGENT_TOKEN, PUBLIC_BASE_URL

logger = logging.getLogger(__name__)


@dataclass
class SendOutcome:
    status: str                     # SENT, READY_NOT_SENT, AWAITING_PULL, FAILED
    provider_message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False


def send_whatsapp(message_id: int, phone10: str, text: str, idempotency_key: str,
                  template_key: str, link: Optional[str]) -> SendOutcome:
    mode = WHATSAPP_MODE
    if mode == "pull":
        return SendOutcome("AWAITING_PULL")
    if mode != "push":
        logger.info("whatsapp_stub message_id=%s to=+91%s chars=%d", message_id, phone10[-4:].rjust(10, "*"), len(text))
        return SendOutcome("READY_NOT_SENT")
    if not WHATSAPP_AGENT_URL or not WHATSAPP_AGENT_TOKEN:
        return SendOutcome("FAILED", error="WHATSAPP_MODE=push but WHATSAPP_AGENT_URL/TOKEN are not set.")

    payload = {
        "to": f"+91{phone10}",
        "text": text,
        "lang": "hi-en",
        "template_key": template_key,
        "link": link,
        "idempotency_key": idempotency_key,
        "callback_url": f"{PUBLIC_BASE_URL}/api/agent/whatsapp/status",
        "message_id": message_id,
    }
    req = urllib.request.Request(
        WHATSAPP_AGENT_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {WHATSAPP_AGENT_TOKEN}", "Content-Type": "application/json",
                 "Idempotency-Key": idempotency_key})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode() or "{}")
        return SendOutcome("SENT", provider_message_id=str(body.get("id") or body.get("message_id") or ""))
    except urllib.error.HTTPError as e:
        return SendOutcome("FAILED", error=f"WhatsApp agent HTTP {e.code}", retryable=e.code >= 500 or e.code == 429)
    except Exception as e:
        return SendOutcome("FAILED", error=f"WhatsApp agent unreachable: {e}", retryable=True)
