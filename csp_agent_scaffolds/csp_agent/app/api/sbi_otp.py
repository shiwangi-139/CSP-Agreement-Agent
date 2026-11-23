"""
FastAPI router for receiving relayed SBI OTPs from the mobile app.

POST /sbi-otp
GET  /sbi-otp/latest
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, Dict, Any

from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger("sbi_otp_router")

router = APIRouter()

DEFAULT_TOKEN = os.getenv("OTP_TOKEN", os.getenv("EKO_SBI_SMS_WEBHOOK_TOKEN", "sR8HLKuRrBdfs52mBBsJkg3o4wG09naV"))
ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("OTP_ALLOWED_SENDERS", "SBIBNK,SBIINB").split(",") if s.strip()]

OTP_TEXT_FILE = Path(os.getenv("OTP_TEXT_FILE", "/tmp/sbi_latest_otp.txt"))
OTP_JSON_FILE = Path(os.getenv("OTP_JSON_FILE", "/tmp/sbi_latest_otp.json"))
OTP_SPOOL_DIR = Path(os.getenv("OTP_SPOOL_DIR", "/home/adarsh/eko/.tmp/sbi_stmt_bot/sms_inbox"))

latest_otp_record: Dict[str, Any] = {
    "otp": None,
    "from": None,
    "text": None,
    "timestamp": 0.0,
    "received_at": None,
}


def extract_otp(text: str) -> Optional[str]:
    """Extract a 6-digit OTP from SBI SMS text."""
    if not text:
        return None

    # Priority 1: "<6-digits> is OTP" (the user's exact format)
    # e.g. "001135  is OTP. Do not share this OTP with anyone. -SBI"
    m = re.search(r'(\d{6})\s+is\s+(?:your\s+)?OTP', text, re.IGNORECASE)
    if m:
        return m.group(1)

    # Priority 2: "OTP ... <6-digits>"
    m = re.search(r'OTP[^\d\n\r]{0,25}(\d{6})', text, re.IGNORECASE)
    if m:
        return m.group(1)

    # Priority 3: any standalone 6-digit number
    matches = re.findall(r'\b(\d{6})\b', text)
    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        return matches[0]

    return None


def save_otp(otp: str, sender: str, raw_text: str) -> Dict[str, Any]:
    now = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    record = {
        "otp": otp,
        "from": sender,
        "text": raw_text,
        "timestamp": now,
        "received_at": now_iso,
    }

    global latest_otp_record
    latest_otp_record = record

    try:
        OTP_TEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OTP_TEXT_FILE.write_text(otp.strip() + "\n", encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write {OTP_TEXT_FILE}: {e}")

    try:
        OTP_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        OTP_JSON_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to write {OTP_JSON_FILE}: {e}")

    return record


async def _handle_otp_post(request: Request, authorization: Optional[str] = None):
    # 1. Bearer Token Check
    if DEFAULT_TOKEN:
        expected = f"Bearer {DEFAULT_TOKEN}"
        if authorization != expected:
            logger.warning(f"Unauthorized OTP POST attempt: {authorization}")
            raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")

    # 2. Parse JSON
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    sender = str(body.get("from", "")).strip()
    text = str(body.get("text", "")).strip()

    logger.info(f"Received SMS relay - Sender: '{sender}'")

    # 3. Check Sender
    sender_lower = sender.lower()
    allowed = any(m in sender_lower for m in ALLOWED_SENDERS)
    if not allowed:
        logger.warning(f"Ignored SMS: Sender '{sender}' not in allowed senders {ALLOWED_SENDERS}")
        return {"status": "ignored", "reason": f"sender '{sender}' not allowed"}

    # 4. Extract OTP
    otp = extract_otp(text)
    if not otp:
        logger.warning("No 6-digit OTP found in SMS text")
        return {"status": "ignored", "reason": "no_otp_found"}

    logger.info(f"Extracted SBI OTP: {otp} from sender '{sender}'")
    record = save_otp(otp, sender, text)

    return {
        "status": "ok",
        "otp": otp,
        "from": sender,
        "received_at": record["received_at"],
    }


def _handle_otp_get(max_age_seconds: float = 300.0):
    global latest_otp_record
    now = time.time()

    # 1. In-memory cached record
    if latest_otp_record.get("otp") and (now - latest_otp_record["timestamp"] <= max_age_seconds):
        res = dict(latest_otp_record)
        res["age_seconds"] = round(now - latest_otp_record["timestamp"], 1)
        res["valid"] = True
        return res

    # 2. Check Adarsh's OTP receiver spool directory (/home/adarsh/eko/.tmp/sbi_stmt_bot/sms_inbox)
    if OTP_SPOOL_DIR.exists() and OTP_SPOOL_DIR.is_dir():
        try:
            files = sorted(
                [f for f in OTP_SPOOL_DIR.glob("*.json") if not f.name.endswith(".yonosbi-consumed") and not f.name.endswith(".csp-consumed")],
                key=lambda p: p.name,
                reverse=True
            )
            for f in files[:20]:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    otp = str(data.get("otp", "")).strip()
                    if not otp or len(otp) != 6 or not otp.isdigit():
                        continue
                    ts = data.get("received_ts") or data.get("timestamp")
                    if not ts:
                        m = re.match(r"^(\d+)", f.stem)
                        ts = float(m.group(1)) / 1000.0 if m else f.stat().st_mtime
                    else:
                        ts = float(ts)
                        if ts > 1e11:
                            ts = ts / 1000.0

                    if now - ts <= max_age_seconds:
                        rec = {
                            "status": "ok",
                            "otp": otp,
                            "from": data.get("from", "SBI"),
                            "text": data.get("text", ""),
                            "timestamp": ts,
                            "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                            "age_seconds": round(now - ts, 1),
                            "valid": True,
                            "source": f"spool:{f.name}",
                        }
                        latest_otp_record = rec
                        return rec
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Error reading spool {OTP_SPOOL_DIR}: {e}")

    # 3. Check /tmp/sbi_latest_otp.json
    if OTP_JSON_FILE.exists():
        try:
            data = json.loads(OTP_JSON_FILE.read_text(encoding="utf-8"))
            ts = data.get("timestamp", 0)
            if now - ts <= max_age_seconds and data.get("otp"):
                data["age_seconds"] = round(now - ts, 1)
                data["valid"] = True
                latest_otp_record = data
                return data
        except Exception:
            pass

    # 4. Check /tmp/sbi_latest_otp.txt
    if OTP_TEXT_FILE.exists():
        try:
            mtime = OTP_TEXT_FILE.stat().st_mtime
            if now - mtime <= max_age_seconds:
                txt = OTP_TEXT_FILE.read_text(encoding="utf-8").strip()
                if txt and len(txt) == 6 and txt.isdigit():
                    rec = {
                        "status": "ok",
                        "otp": txt,
                        "from": "SBI",
                        "timestamp": mtime,
                        "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
                        "age_seconds": round(now - mtime, 1),
                        "valid": True,
                        "source": "text_file",
                    }
                    latest_otp_record = rec
                    return rec
        except Exception:
            pass

    raise HTTPException(status_code=404, detail="No fresh OTP received within the last 5 minutes")


# Support both /sbi-otp and /csp-agreement-agent/sbi-otp (in case reverse proxy passes full path)
@router.post("/sbi-otp")
@router.post("/csp-agreement-agent/sbi-otp")
async def receive_sbi_otp(request: Request, authorization: Optional[str] = Header(None)):
    return await _handle_otp_post(request, authorization)


@router.get("/sbi-otp")
@router.get("/sbi-otp/latest")
@router.get("/csp-agreement-agent/sbi-otp")
@router.get("/csp-agreement-agent/sbi-otp/latest")
def get_latest_sbi_otp(max_age_seconds: float = 300.0):
    return _handle_otp_get(max_age_seconds)
