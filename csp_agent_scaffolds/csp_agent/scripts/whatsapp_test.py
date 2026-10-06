"""
End-to-end WhatsApp TEST: send real queued drafts to YOUR OWN number(s)
through Eko's WhatsApp Bulk Sender, never to the CSPs. The drafts stay in
the queue untouched.

    python -m scripts.whatsapp_test whoami
    python -m scripts.whatsapp_test send --to 9198XXXXXXXX --kind onboard --count 2
    python -m scripts.whatsapp_test send --to 9198XXXXXXXX,9197XXXXXXXX --kind missing
    python -m scripts.whatsapp_test history
    python -m scripts.whatsapp_test status --job JOB_ID

--kind: onboard (no documents) · missing · expired · renewal · unreadable
--to:   one or more numbers with country code, comma-separated (91XXXXXXXXXX)

How sending works (Bulk Sender rules): every WhatsApp bulk message must use
a template Meta has approved. The first send of a new wording returns
"created"/"pending": run the SAME command again later (minutes to a day)
and it sends as soon as Meta approves. Messages are sent as UTILITY.

Connection: WABS_MCP_URL (default https://indev.eko.in/whatsapp/mcp), the
platform's hosted endpoint, which carries the team account itself. If your
own account gives you a separate address or key, set WABS_MCP_URL and
WABS_API_KEY in .env.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

from app.db import SessionLocal
from app.models import CSP, OutboundMessage, OutboundStatus

URL = os.getenv("WABS_MCP_URL", "https://indev.eko.in/whatsapp/mcp")
KEY = os.getenv("WABS_API_KEY", "")
KINDS = {"onboard": ["ONBOARD_ALL"], "missing": ["UPLOAD_MISSING"], "expired": ["UPLOAD_EXPIRED"],
         "renewal": ["RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL"], "unreadable": ["UNREADABLE_REUPLOAD"]}


def call(tool: str, args: dict) -> dict:
    """One MCP tool call (JSON-RPC over streamable HTTP)."""
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    with urllib.request.urlopen(urllib.request.Request(URL, data=body, headers=headers, method="POST"),
                                timeout=180) as r:
        raw = r.read().decode()
    payload = json.loads(re.search(r"\{.*\}", raw.replace("data: ", ""), re.S).group(0))
    if "error" in payload:
        raise SystemExit(f"Bulk Sender error: {payload['error']}")
    result = payload.get("result", {})
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except ValueError:
                return {"text": part["text"]}
    return result


def _numbers(raw: str) -> list[str]:
    out = []
    for n in raw.split(","):
        n = re.sub(r"\D", "", n)
        if len(n) == 10:
            n = "91" + n
        if not re.fullmatch(r"91[6-9]\d{9}", n):
            raise SystemExit(f"Not a valid Indian mobile number: {n!r} (use 91XXXXXXXXXX)")
        out.append(n)
    return out


def _flat(text: str) -> str:
    # Meta refuses line breaks inside template values; keep the reading order with " · ".
    return re.sub(r"\s*\n+\s*", " · ", text.strip())


def send(to: list[str], kind: str, count: int) -> None:
    db = SessionLocal()
    try:
        drafts = (db.query(OutboundMessage).filter(OutboundMessage.status == OutboundStatus.QUEUED_FOR_REVIEW,
                                                   OutboundMessage.channel == "WHATSAPP",
                                                   OutboundMessage.recipient_role == "CSP",
                                                   OutboundMessage.template_name.in_(KINDS[kind]))
                  .order_by(OutboundMessage.id).limit(count).all())
        if not drafts:
            raise SystemExit(f"No queued WhatsApp drafts of type {kind!r}.")
        contacts = []
        for i, m in enumerate(drafts):
            csp = db.get(CSP, m.csp_id)
            number = to[i % len(to)]
            body = _flat((m.payload_json or {}).get("body", ""))
            link = re.search(r"https?://\S+", body)
            rm = re.search(r"RM ([^\s(·]+)", body)
            # The sender finds what varies per person by comparing the message
            # with these columns, so one template fits every row.
            contacts.append({"phone": number, "name": csp.name if csp else "CSP",
                             "code": csp.current_code if csp else "", "link": link.group(0) if link else "",
                             "rm": rm.group(1) if rm else "", "message": body})
            print(f"  draft {m.id} ({m.template_name}, {csp.current_code if csp else '-'}) -> {number}")
    finally:
        db.close()
    print(f"Sending {len(contacts)} TEST message(s) to {', '.join(sorted(set(to)))} (never to the CSPs)...")
    r = call("smart_send_from_messages", {"contacts": contacts, "message_column": "message", "phone_column": "phone",
                                          "category": "UTILITY", "language": "en_US",
                                          "source_name": f"csp-agent-test-{kind}"})
    print(json.dumps(r, indent=2, ensure_ascii=False)[:3000])
    action = r.get("action")
    if action == "sent":
        print("\nSent. Check WhatsApp on the test phone; then: python -m scripts.whatsapp_test history")
    elif action in ("created", "pending"):
        print("\nMeta is reviewing the template. Run the SAME command again later; it sends once approved.")
    elif action == "rejected":
        print("\nMeta rejected the wording:", r.get("reason"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["whoami", "send", "history", "status"])
    ap.add_argument("--to", help="your number(s), 91XXXXXXXXXX, comma-separated")
    ap.add_argument("--kind", choices=sorted(KINDS), default="onboard")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--job")
    a = ap.parse_args()
    if a.action == "whoami":
        print(json.dumps(call("whoami", {}), indent=2))
    elif a.action == "history":
        print(json.dumps(call("get_send_history", {}), indent=2, ensure_ascii=False)[:4000])
    elif a.action == "status":
        if not a.job:
            sys.exit("--job is required (from the send result or history)")
        print(json.dumps(call("get_delivery_status", {"job_id": a.job}), indent=2)[:4000])
    else:
        if not a.to:
            sys.exit("--to is required: the test number(s), never a CSP's")
        send(_numbers(a.to), a.kind, max(1, min(a.count, 10)))


if __name__ == "__main__":
    main()
