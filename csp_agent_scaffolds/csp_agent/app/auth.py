"""
API keys.

  X-API-Key: ADMIN_API_KEY   dashboard + all /api/* routes except the two below
  X-Agent-Key: AGENT_API_KEY  /api/agent/* (the WhatsApp agent's pull API)
  public                      /upload, /api/portal/*, /health, the static
                              dashboard page itself (it holds no data)

If ADMIN_API_KEY is empty the admin routes stay open so the app works on a
laptop before .env is updated; a warning is logged at startup. Set it
before the server is reachable from anywhere else.
"""
import hmac
import logging

from fastapi import Header, HTTPException

from .config import ADMIN_API_KEY, AGENT_API_KEY

logger = logging.getLogger(__name__)


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def require_admin(x_api_key: str = Header(default="")) -> str:
    if not ADMIN_API_KEY:
        return "local-dev"
    if not x_api_key or not _eq(x_api_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-API-Key.")
    return "admin"


def require_agent(x_agent_key: str = Header(default=""), authorization: str = Header(default="")) -> str:
    """The WhatsApp agent sends AGENT_API_KEY as X-Agent-Key, or as
    "Authorization: Bearer <key>" (easier for delivery callbacks)."""
    if not AGENT_API_KEY:
        raise HTTPException(status_code=503, detail="AGENT_API_KEY is not configured.")
    key = x_agent_key or (authorization[7:].strip() if authorization[:7].lower() == "bearer " else "")
    if not key or not _eq(key, AGENT_API_KEY):
        raise HTTPException(status_code=401, detail="Missing or wrong X-Agent-Key.")
    return "whatsapp-agent"


def warn_if_open() -> None:
    if not ADMIN_API_KEY:
        logger.warning("ADMIN_API_KEY is not set: dashboard and API are open. Set it in .env before exposing the server.")
