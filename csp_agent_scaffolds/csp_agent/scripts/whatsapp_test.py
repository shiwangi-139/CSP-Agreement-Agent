"""
End-to-end WhatsApp TEST: send real queued drafts to YOUR OWN number(s)
through Eko's WhatsApp Bulk Sender, never to the CSPs. The drafts stay in
the queue untouched.

    python -m scripts.whatsapp_test whoami
    python -m scripts.whatsapp_test send --to 9198XXXXXXXX --kind onboard --count 2
    python -m scripts.whatsapp_test send --to 9198XXXXXXXX,9197XXXXXXXX --kind missing
    python -m scripts.whatsapp_test send --to 9198XXXXXXXX --kind onboard --force   # resend within 15 min
    python -m scripts.whatsapp_test template                 # has Meta approved our test wording?
    python -m scripts.whatsapp_test submit --kind link       # ask Meta to approve a layout, sending nothing
    python -m scripts.whatsapp_test history                  # only this tool's sends (the account is shared)
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
import re
import sys

from app.comms.wabs import CLEAN, call, send_clean
from app.db import SessionLocal
from app.models import CSP, OutboundMessage, OutboundStatus

KINDS = {"onboard": ["ONBOARD_ALL"], "missing": ["UPLOAD_MISSING"], "expired": ["UPLOAD_EXPIRED"],
         "renewal": ["RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL"], "unreadable": ["UNREADABLE_REUPLOAD"]}


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


def send(to: list[str], kind: str, count: int, style: str = "clean", force: bool = False) -> None:
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
            rm_text = f"RM {rm.group(1)}" if rm else "आपके RM / your RM"
            # The sender finds what varies per person by comparing the message
            # with these columns, so one template fits every row.
            contacts.append({"phone": number, "name": csp.name if csp else "CSP",
                             "code": csp.current_code if csp else "", "link": link.group(0) if link else "",
                             "rm": rm_text, "message": body})
            print(f"  draft {m.id} ({m.template_name}, {csp.current_code if csp else '-'}) -> {number}")
    finally:
        db.close()
    print(f"Sending {len(contacts)} TEST message(s) to {', '.join(sorted(set(to)))} (never to the CSPs)...")
    # The Bulk Sender refuses the same message to the same number within 15
    # minutes; --force overrides that (only ever for these test numbers).
    extra = {"force": True} if force else {}
    if style == "clean" and kind in CLEAN:
        return _report(send_clean(kind, [{k: v for k, v in c.items() if k != "message"} for c in contacts],
                                  f"csp-agent-test-{kind}-clean", force))
    r = call("smart_send_from_messages", {"contacts": contacts, "message_column": "message", "phone_column": "phone",
                                          "category": "UTILITY", "language": "en_US",
                                          "source_name": f"csp-agent-test-{kind}", **extra})
    _report(r)


def _report(r: dict) -> None:
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
    ap.add_argument("action", choices=["whoami", "send", "history", "status", "template", "submit"])
    ap.add_argument("--to", help="your number(s), 91XXXXXXXXXX, comma-separated")
    ap.add_argument("--kind", choices=sorted(set(KINDS) | set(CLEAN)), default="onboard")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--job")
    ap.add_argument("--style", choices=["clean", "plain"], default="clean",
                    help="clean: fixed template with line breaks (onboard); plain: the draft text as one block")
    ap.add_argument("--force", action="store_true",
                    help="send even if the same message went to this number in the last 15 minutes")
    a = ap.parse_args()
    if a.action == "whoami":
        print(json.dumps(call("whoami", {}), indent=2))
    elif a.action == "history":
        h = call("get_send_history", {})
        rows = h if isinstance(h, list) else h.get("history") or h.get("sends") or [h]
        ours = [r for r in rows if isinstance(r, dict) and "csp-agent" in str(r.get("sheet_name") or r.get("source_name") or "")]
        if not ours:
            print("No sends from this tool yet (a template still waiting for Meta has not sent anything).")
        for r in ours:
            print(f"  {r.get('timestamp', '')[:19]}  job {r.get('job_id')}  {r.get('template_name')}  "
                  f"sent {r.get('sent')} · failed {r.get('failed')} · skipped {r.get('skipped')}  ({r.get('sheet_name')})")
    elif a.action == "submit":
        if a.kind not in CLEAN:
            sys.exit(f"No fixed layout for {a.kind!r} yet (layouts: {', '.join(CLEAN)}).")
        r = call("create_meta_template", {"name": f"csp_agent_{a.kind}", "body": CLEAN[a.kind],
                                          "category": "UTILITY", "language": "hi"})
        print(json.dumps(r, indent=2, ensure_ascii=False)[:1500])
        print("\nSubmitted. Check with: python -m scripts.whatsapp_test template")
    elif a.action == "template":
        t = call("list_meta_templates", {})
        rows = t if isinstance(t, list) else t.get("templates") or t.get("data") or []
        ours = [x for x in rows if isinstance(x, dict) and str(x.get("name", "")).startswith(("auto_", "csp_agent_"))]
        for x in ours[-10:]:
            print(f"  {x.get('name')}: {x.get('status')}  {x.get('category', '')}  {str(x.get('rejected_reason') or '')}")
        if not ours:
            print(json.dumps(t, indent=2)[:1500])
    elif a.action == "status":
        if not a.job:
            sys.exit("--job is required (from the send result or history)")
        print(json.dumps(call("get_delivery_status", {"job_id": a.job}), indent=2)[:4000])
    else:
        if not a.to:
            sys.exit("--to is required: the test number(s), never a CSP's")
        send(_numbers(a.to), a.kind, max(1, min(a.count, 10)), a.style, a.force)


if __name__ == "__main__":
    main()
