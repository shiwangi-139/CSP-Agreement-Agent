"""
app/comms/wabs.py
Eko's WhatsApp Bulk Sender (MCP endpoint, JSON-RPC tools/call), used by
WHATSAPP_MODE=wabs and by scripts/whatsapp_test.py.

Every bulk WhatsApp message must use a template Meta has approved. Each
message type that may go out has a fixed layout below (real line breaks;
only the {{n}} values change per CSP). A type without a layout here cannot
be sent on WhatsApp yet.
"""
import json
import os
import re
import urllib.request

URL = os.getenv("WABS_MCP_URL", "https://indev.eko.in/whatsapp/mcp")
KEY = os.getenv("WABS_API_KEY", "")

# Clean layout per message kind (Meta allows no line breaks inside a value).
CLEAN = {
    "onboard": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\n"
        "हमारे रिकॉर्ड में अभी आपके सीएसपी डॉक्यूमेंट जमा नहीं हैं। अगर आपने ये पहले अपने RM को दिए थे, "
        "तो हो सकता है वो हमारे रिकॉर्ड में नहीं आए।\n\n"
        "ये तीनों डॉक्यूमेंट जमा करना ज़रूरी है:\n"
        "• सीएसपी एग्रीमेंट\n"
        "• पुलिस वेरिफिकेशन / चरित्र प्रमाण पत्र\n"
        "• आईआईबीएफ सर्टिफिकेट\n\n"
        "फॉर्म में आपकी कोई जानकारी खाली या गलत हो तो सही भर दें। हमारी टीम पूरी जाँच के बाद ही उसे बदलेगी।\n\n"
        "Hello {{3}},\n"
        "Your CSP documents are not on our records yet. If you gave them to your RM earlier, "
        "they may not have reached us.\n\n"
        "All three documents above are mandatory.\n\n"
        "If any detail in the form is missing or wrong, please correct it. Our team checks every change.\n\n"
        "📎 अपलोड करें / Upload here:\n{{4}}\n\n"
        "मदद / Help: {{5}}\n"
        "हर पेज की साफ़ फोटो लें, या PDF डालें / A clear photo of each page, or a PDF.\n"
        "— Eko"),
}
# The CSP asked for their link on the public page (sent only to the number on record).
CLEAN["link"] = (
    "नमस्ते {{1}} (KO {{2}}) 🙏\n\n"
    "आपने डॉक्यूमेंट अपलोड करने का लिंक माँगा था। यह लिंक सिर्फ़ आपके लिए है, इसे किसी और को न भेजें।\n"
    "You asked for your document upload link. It is only for you, please don't share it.\n\n"
    "📎 अपलोड करें / Upload here:\n{{3}}\n\n"
    "अगर आपने लिंक नहीं माँगा था, तो इस मैसेज को अनदेखा करें। / If you didn't ask for it, please ignore this message.\n"
    "— Eko")
CLEAN_MAP = {"onboard": {"1": "name", "2": "code", "3": "name", "4": "link", "5": "rm"},
             "link": {"1": "name", "2": "code", "3": "link"}}
# Message type (OutboundMessage.template_name) -> layout above.
KIND_FOR_TEMPLATE = {"ONBOARD_ALL": "onboard", "LINK_REQUEST": "link"}


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
        raise RuntimeError(f"Bulk Sender error: {payload['error']}")
    result = payload.get("result", {})
    if isinstance(result.get("structuredContent"), dict) and "result" in result["structuredContent"]:
        return result["structuredContent"]["result"]
    parts = []
    for part in result.get("content", []):
        if part.get("type") == "text":
            try:
                parts.append(json.loads(part["text"]))
            except ValueError:
                parts.append({"text": part["text"]})
    if not parts:
        return result
    return parts[0] if len(parts) == 1 else parts


def send_clean(kind: str, contacts: list[dict], source_name: str, force: bool = False) -> dict:
    """Send the `kind` layout to `contacts` (dicts with phone 91XXXXXXXXXX and
    the CLEAN_MAP columns). Returns the Bulk Sender's answer; its "action" is
    sent, created/pending (Meta still reviewing) or rejected."""
    job = call("upload_contacts", {"contacts": contacts, "source_name": source_name})
    r = call("smart_send_template", {"job_id": job["job_id"], "phone_column": "phone", "body": CLEAN[kind],
                                     "mapping": {n: {"type": "column", "value": col}
                                                 for n, col in CLEAN_MAP[kind].items()},
                                     "category": "UTILITY", "language": "hi",
                                     **({"force": True} if force else {})})
    if isinstance(r, dict):
        r.setdefault("job_id", job["job_id"])    # for get_delivery_status later
    return r
