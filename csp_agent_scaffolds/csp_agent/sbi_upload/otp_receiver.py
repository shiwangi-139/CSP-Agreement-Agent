#!/usr/bin/env python3
"""
SBI OTP Receiver Service.

Receives forwarded OTP SMS from the Android `sbi_otp_relay_app` via HTTP POST /sbi-otp,
extracts the 6-digit OTP, and stores it in memory and persistent files for automation scripts.

Supported Sender: SBIBNK, SBIINB (case-insensitive)
Sample Message Format: "001135  is OTP. Do not share this OTP with anyone. -SBI"

Can be run standalone:
    python3 sbi_upload/otp_receiver.py --port 8080
    python3 sbi_upload/otp_receiver.py --test
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("sbi_otp_receiver")

# Default token matching Config.java baked into the relay app
DEFAULT_TOKEN = os.getenv("OTP_TOKEN", os.getenv("EKO_SBI_SMS_WEBHOOK_TOKEN", "sR8HLKuRrBdfs52mBBsJkg3o4wG09naV"))
ALLOWED_SENDERS = [s.strip().lower() for s in os.getenv("OTP_ALLOWED_SENDERS", "SBIBNK,SBIINB").split(",") if s.strip()]

# Persistent file paths
OTP_TEXT_FILE = Path(os.getenv("OTP_TEXT_FILE", "/tmp/sbi_latest_otp.txt"))
OTP_JSON_FILE = Path(os.getenv("OTP_JSON_FILE", "/tmp/sbi_latest_otp.json"))

# Global in-memory state
latest_otp_record: Dict[str, Any] = {
    "otp": None,
    "from": None,
    "text": None,
    "timestamp": 0.0,
    "received_at": None,
}


def extract_otp(text: str) -> Optional[str]:
    """
    Extract a 6-digit OTP from the SMS text.
    Handles various SBI formats:
      - '001135  is OTP. Do not share this OTP with anyone. -SBI'
      - '001135 is your One Time Password (OTP)...'
      - 'OTP is 001135'
    """
    if not text:
        return None

    # Priority 1: "<6-digits> is OTP" (the user's exact format)
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
        # Return first 6-digit match
        return matches[0]

    return None


def save_otp(otp: str, sender: str, raw_text: str) -> Dict[str, Any]:
    """Store the extracted OTP in memory and on disk."""
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

    # Write simple text file (for easy reading via cat or file watch)
    try:
        OTP_TEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
        OTP_TEXT_FILE.write_text(otp.strip() + "\n", encoding="utf-8")
        logger.info(f"Wrote OTP to {OTP_TEXT_FILE}")
    except Exception as e:
        logger.warning(f"Failed to write OTP text file {OTP_TEXT_FILE}: {e}")

    # Write JSON metadata file
    try:
        OTP_JSON_FILE.parent.mkdir(parents=True, exist_ok=True)
        OTP_JSON_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
        logger.info(f"Wrote OTP metadata to {OTP_JSON_FILE}")
    except Exception as e:
        logger.warning(f"Failed to write OTP JSON file {OTP_JSON_FILE}: {e}")

    return record


def get_latest_otp(max_age_seconds: float = 300.0) -> Optional[Dict[str, Any]]:
    """Retrieve the latest OTP if it's within the freshness window."""
    global latest_otp_record
    now = time.time()

    # Try in-memory first
    if latest_otp_record.get("otp") and (now - latest_otp_record["timestamp"] <= max_age_seconds):
        res = dict(latest_otp_record)
        res["age_seconds"] = round(now - latest_otp_record["timestamp"], 1)
        res["valid"] = True
        return res

    # Fallback to reading JSON file from disk (in case receiver is another process)
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

    # Fallback to reading text file from disk
    if OTP_TEXT_FILE.exists():
        try:
            mtime = OTP_TEXT_FILE.stat().st_mtime
            if now - mtime <= max_age_seconds:
                content = OTP_TEXT_FILE.read_text(encoding="utf-8").strip()
                if content and len(content) == 6 and content.isdigit():
                    return {
                        "otp": content,
                        "from": "UNKNOWN",
                        "timestamp": mtime,
                        "age_seconds": round(now - mtime, 1),
                        "valid": True,
                    }
        except Exception:
            pass

    return None


class OTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for standalone OTP receiver."""

    def _send_json(self, status_code: int, data: Dict[str, Any]):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")

        if path in ("", "/health", "/ready"):
            self._send_json(200, {
                "status": "alive",
                "service": "sbi_otp_receiver",
                "senders_allowed": ALLOWED_SENDERS
            })
            return

        if path in ("/sbi-otp", "/sbi-otp/latest", "/api/otp/latest"):
            otp_data = get_latest_otp(max_age_seconds=600)
            if otp_data:
                self._send_json(200, otp_data)
            else:
                self._send_json(404, {"error": "no_fresh_otp", "detail": "No valid OTP received within the last 10 minutes"})
            return

        self._send_json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")

        if path != "/sbi-otp":
            self._send_json(404, {"error": "not_found", "path": self.path})
            return

        # 1. Verify Authorization Header (Bearer token)
        auth_header = self.headers.get("Authorization", "")
        if DEFAULT_TOKEN:
            expected = f"Bearer {DEFAULT_TOKEN}"
            if auth_header != expected:
                logger.warning(f"Unauthorized OTP POST attempt: {auth_header}")
                self._send_json(401, {"error": "unauthorized", "detail": "Invalid or missing Bearer token"})
                return

        # 2. Read body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length).decode("utf-8")
            payload = json.loads(raw_body)
        except Exception as e:
            logger.error(f"Failed to read/parse request JSON: {e}")
            self._send_json(400, {"error": "bad_request", "detail": str(e)})
            return

        sender = str(payload.get("from", "")).strip()
        text = str(payload.get("text", "")).strip()

        logger.info(f"Received SMS relay - Sender: '{sender}'")

        # 3. Check Sender
        sender_lower = sender.lower()
        allowed = any(m in sender_lower for m in ALLOWED_SENDERS)
        if not allowed:
            logger.warning(f"Ignored SMS: Sender '{sender}' not in allowed senders {ALLOWED_SENDERS}")
            self._send_json(200, {"status": "ignored", "reason": f"sender '{sender}' not allowed"})
            return

        # 4. Extract OTP
        otp = extract_otp(text)
        if not otp:
            logger.warning("No 6-digit OTP found in SMS text")
            self._send_json(200, {"status": "ignored", "reason": "no_otp_found"})
            return

        logger.info(f"Extracted SBI OTP: {otp} from sender '{sender}'")
        record = save_otp(otp, sender, text)

        self._send_json(200, {
            "status": "ok",
            "otp": otp,
            "from": sender,
            "received_at": record["received_at"]
        })

    def log_message(self, format, *args):
        # Override standard BaseHTTPRequestHandler log to use Python logger
        logger.debug("%s - - [%s] %s" % (self.client_address[0], self.log_date_time_string(), format % args))


def run_server(port: int = 8080, host: str = "0.0.0.0"):
    """Start standalone OTP HTTP server."""
    server_address = (host, port)
    httpd = HTTPServer(server_address, OTPRequestHandler)
    logger.info(f"SBI OTP Receiver server started on http://{host}:{port}/sbi-otp")
    logger.info(f"Allowed senders: {ALLOWED_SENDERS}")
    logger.info(f"Output files: {OTP_TEXT_FILE} and {OTP_JSON_FILE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping OTP receiver server...")
        httpd.server_close()


def run_self_test():
    """Run verification self-test with the user's sample message."""
    sample_text = "001135  is OTP. Do not share this OTP with anyone. -SBI"
    sample_sender = "SBIBNK"

    print("=" * 60)
    print("RUNNING SBI OTP RECEIVER SELF-TEST")
    print(f"Sample Sender: {sample_sender}")
    print(f"Sample Message: {sample_text}")
    print("=" * 60)

    otp = extract_otp(sample_text)
    assert otp == "001135", f"Expected '001135' but got '{otp}'"
    print(f"[PASS] Successfully extracted OTP: {otp}")

    record = save_otp(otp, sample_sender, sample_text)
    assert record["otp"] == "001135"
    print(f"[PASS] Successfully saved OTP to {OTP_TEXT_FILE} and {OTP_JSON_FILE}")

    latest = get_latest_otp(max_age_seconds=10)
    assert latest is not None and latest["otp"] == "001135"
    print(f"[PASS] Successfully retrieved fresh OTP: {latest}")

    print("=" * 60)
    print("ALL TESTS PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SBI OTP Relay Receiver Server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", os.getenv("OTP_PORT", 8080))), help="HTTP port to listen on (default: 8080 or $PORT)")
    parser.add_argument("--host", type=str, default=os.getenv("HOST", "0.0.0.0"), help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--test", action="store_true", help="Run self-test with sample message and exit")

    args = parser.parse_args()

    if args.test:
        run_self_test()
    else:
        run_server(port=args.port, host=args.host)
