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
        "फॉर्म में आपका नाम, मोबाइल और RM हमारे रिकॉर्ड से भरे हैं। कुछ गलत हो तो अपने RM को बताएँ।\n\n"
        "Hello {{3}},\n"
        "Your CSP documents are not on our records yet. If you gave them to your RM earlier, "
        "they may not have reached us.\n\n"
        "All three documents above are mandatory.\n\n"
        "Your name, mobile and RM in the form come from our records. If anything is wrong, please tell your RM.\n\n"
        "📎 अपलोड करें / Upload here:\n{{4}}\n\n"
        "मदद / Help: {{5}}\n"
        "कृपया साफ़ स्कैन की हुई PDF भेजें / Please send a clear scanned PDF.\n"
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
# The other CSP messages (same Hindi-first wording as app/comms/templates.py).
# A layout Meta hasn't approved yet is submitted by the Bulk Sender on first
# use; the message waits ("Waiting for Meta…") and goes out once approved.
_END = ("\n\n📎 अपलोड करें / Upload here:\n{link}\n\nमदद / Help: {rm}\n"
        "कृपया साफ़ स्कैन की हुई PDF या साफ़ फोटो भेजें / Please send a clear scanned PDF or photo.\n— Eko")
CLEAN.update({
    "renewal_notice": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nआपका {{3}} {{4}} तक ही मान्य है।\n"
        "कृपया समय रहते इसे रिन्यू करवाकर नया डॉक्यूमेंट अपलोड करें, ताकि आपका काम बिना रुके चलता रहे।\n\n"
        "Hello {{5}},\nYour {{6}} is valid only until {{7}}. Please renew it in time and upload the new document."
        + _END.format(link="{{8}}", rm="{{9}}")),
    "renewal_reminder": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nरिमाइंडर: आपका {{3}} {{4}} तक ही मान्य है, और हमें अभी तक नया डॉक्यूमेंट नहीं मिला है।\n"
        "कृपया इसे रिन्यू करवाकर नया डॉक्यूमेंट जल्दी अपलोड करें।\n\n"
        "Hello {{5}},\nReminder: your {{6}} is valid only until {{7}} and we have not received the renewed document yet. "
        "Please upload it soon." + _END.format(link="{{8}}", rm="{{9}}")),
    "renewal_final": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nज़रूरी: आपका {{3}} सिर्फ़ {{4}} दिन और ({{5}} तक) मान्य है।\n"
        "इसके बाद आपका टर्मिनल रुक सकता है, इसलिए कृपया जल्दी से नया डॉक्यूमेंट अपलोड करें।\n\n"
        "Hello {{6}},\nUrgent: your {{7}} expires in {{8}} day(s). Your terminal may be blocked after expiry. "
        "Please upload the renewed document now." + _END.format(link="{{9}}", rm="{{10}}")),
    "expired": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nआपके इन डॉक्यूमेंट की तारीख निकल चुकी है: {{3}}।\n"
        "कृपया इन्हें रिन्यू करवाकर नए डॉक्यूमेंट अपलोड करें। पुराने डॉक्यूमेंट की वजह से आपका टर्मिनल रुक सकता है।\n\n"
        "Hello {{4}},\nThese documents of yours have expired: {{5}}. Please renew them and upload the new documents."
        + _END.format(link="{{6}}", rm="{{7}}")),
    "missing": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nये डॉक्यूमेंट अभी हमारे रिकॉर्ड में जमा नहीं हैं: {{3}}।\n"
        "अगर आपने ये पहले अपने RM को दिए थे, तो हो सकता है वो हमारे रिकॉर्ड में नहीं आए। ये डॉक्यूमेंट जमा करना ज़रूरी है।\n\n"
        "Hello {{4}},\nThese documents are not on our records yet: {{5}}. They are mandatory; please upload them."
        + _END.format(link="{{6}}", rm="{{7}}")),
    "unreadable": (
        "नमस्ते {{1}} (KO {{2}}) 🙏\n\nआपका {{3}} जो हमें मिला था, वो साफ़ पढ़ा नहीं जा सका (फोटो धुंधली है या पूरी नहीं है)।\n"
        "ये डॉक्यूमेंट जमा करना ज़रूरी है। कृपया अच्छी रोशनी में साफ़ फोटो या स्कैन की हुई PDF फिर से अपलोड करें।\n\n"
        "Hello {{4}},\nThe {{5}} we received could not be read (blurry or incomplete). It is mandatory; please upload a "
        "clear copy again." + _END.format(link="{{6}}", rm="{{7}}")),
})
_RENEWAL = {"1": "name", "2": "code", "3": "doc_hi", "4": "date", "5": "name", "6": "doc_en", "7": "date",
            "8": "link", "9": "rm"}
_DOCS = {"1": "name", "2": "code", "3": "doc_hi", "4": "name", "5": "doc_en", "6": "link", "7": "rm"}
CLEAN_MAP = {"onboard": {"1": "name", "2": "code", "3": "name", "4": "link", "5": "rm"},
             "link": {"1": "name", "2": "code", "3": "link"},
             "renewal_notice": _RENEWAL, "renewal_reminder": _RENEWAL,
             "renewal_final": {"1": "name", "2": "code", "3": "doc_hi", "4": "days", "5": "date", "6": "name",
                               "7": "doc_en", "8": "days", "9": "link", "10": "rm"},
             "expired": _DOCS, "missing": _DOCS, "unreadable": _DOCS}
# Message type (OutboundMessage.template_name) -> layout above. RM / DC
# escalations go to staff and have no WhatsApp layout (yet).
KIND_FOR_TEMPLATE = {"ONBOARD_ALL": "onboard", "LINK_REQUEST": "link",
                     "RENEWAL_NOTICE": "renewal_notice", "RENEWAL_FOLLOWUP": "renewal_reminder",
                     "RENEWAL_FINAL": "renewal_final", "UPLOAD_EXPIRED": "expired",
                     "UPLOAD_MISSING": "missing", "UNREADABLE_REUPLOAD": "unreadable"}


def layout_values(template_key: str, csp, state, document_type: str) -> dict:
    """The per-CSP values a layout needs besides name / code / link / rm, from
    the CSP's documents as they are NOW (so a reminder never shows a stale
    date). `document_type` is the message's own ("AGREEMENT" or
    "AGREEMENT,POLICE_VERIFICATION"). Raises ValueError if a value is missing."""
    from ..compliance import DOC_LABELS
    asked = [t for t in (document_type or "").split(",") if t in DOC_LABELS]
    kind = KIND_FOR_TEMPLATE.get(template_key)
    if kind in ("renewal_notice", "renewal_reminder", "renewal_final"):
        if not asked:
            raise ValueError("renewal message without a document type")
        s = state.docs[asked[0]]
        if s.expiry_date is None:
            raise ValueError(f"no expiry date on record for {asked[0]}")
        return {"doc_en": DOC_LABELS[asked[0]][0], "doc_hi": DOC_LABELS[asked[0]][1],
                "date": s.expiry_date.strftime("%d-%m-%Y"), "days": str(max(s.days_left or 0, 0))}
    if kind in ("expired", "missing", "unreadable"):
        now = state.expired if kind == "expired" else state.needs_upload
        types = [t for t in asked if t in now] or asked or list(now)
        if kind == "unreadable":
            types = types[:1]
        if not types:
            raise ValueError("no document to ask for")
        return {"doc_en": ", ".join(DOC_LABELS[t][0] for t in types),
                "doc_hi": ", ".join(DOC_LABELS[t][1] for t in types)}
    return {}


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
