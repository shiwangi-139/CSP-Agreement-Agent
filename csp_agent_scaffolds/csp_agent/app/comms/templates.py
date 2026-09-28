"""
Bilingual message templates: Hindi first, English below, in a short
WhatsApp form and a longer email form.

render(key, channel, ctx) -> {"subject": ..., "body": ...}

ctx keys (all optional except csp_name/csp_code):
  csp_name, csp_code, docs (list of {label_en, label_hi, status, issue, expiry,
  days_left}), doc_label_en/hi (the one document a renewal is about),
  expiry, days_left, upload_link, rm_name, rm_phone, dc_name, stage,
  csp_phone, reason, attempt
"""
from typing import Any

SCAN_RULES_HI = (
    "अपलोड के नियम:\n"
    "• स्कैन की हुई PDF ही अपलोड करें (मोबाइल से: Google Drive → Scan, या Adobe Scan ऐप, या कैमरा का 'Document' मोड)।\n"
    "• सभी पेज पूरे और सीधे हों, कोई हिस्सा कटा न हो।\n"
    "• अच्छी रोशनी में, धुंधला (blur) न हो। स्क्रीन की फोटो न लें।"
)
SCAN_RULES_EN = (
    "Upload rules:\n"
    "• Upload a scanned PDF (on your phone: Google Drive → Scan, the Adobe Scan app, or the camera's 'Document' mode).\n"
    "• All pages, complete and straight, nothing cut off.\n"
    "• Good light, not blurry. Do not photograph a screen."
)

STATUS_HI = {"VALID": "मान्य", "EXPIRED": "समाप्त (Expired)", "MISSING": "प्राप्त नहीं",
             "UNREADABLE": "पढ़ने योग्य नहीं (धुंधला)"}
STATUS_EN = {"VALID": "Valid", "EXPIRED": "Expired", "MISSING": "Not received",
             "UNREADABLE": "Not readable (blurry)"}


def _doc_lines(ctx: dict, lang: str) -> str:
    lines = []
    for d in ctx.get("docs") or []:
        label = d["label_hi"] if lang == "hi" else d["label_en"]
        status = (STATUS_HI if lang == "hi" else STATUS_EN).get(d["status"], d["status"])
        extra = ""
        if d.get("expiry"):
            extra = f" ({'समाप्ति' if lang == 'hi' else 'expiry'}: {d['expiry']})"
        lines.append(f"• {label}: {status}{extra}")
    return "\n".join(lines)


def _needed(ctx: dict, lang: str) -> str:
    key = "label_hi" if lang == "hi" else "label_en"
    names = [d[key] for d in ctx.get("docs") or [] if d["status"] != "VALID"]
    return ", ".join(names)


def _contact(ctx: dict, lang: str) -> str:
    if not ctx.get("rm_name"):
        return ""
    phone = f" ({ctx['rm_phone']})" if ctx.get("rm_phone") else ""
    return (f"सहायता के लिए अपने RM {ctx['rm_name']}{phone} से संपर्क करें।" if lang == "hi"
            else f"For help, contact your RM {ctx['rm_name']}{phone}.")


def _link(ctx: dict, lang: str) -> str:
    if not ctx.get("upload_link"):
        return ""
    return (f"यहाँ अपलोड करें: {ctx['upload_link']}" if lang == "hi"
            else f"Upload here: {ctx['upload_link']}")


# Each builder returns (subject, hindi_block, english_block).
def _renewal_notice(c):
    return (f"Renewal due: {c['doc_label_en']} expires on {c['expiry']} | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\n"
            f"आपका {c['doc_label_hi']} {c['expiry']} को समाप्त हो रहा है ({c['days_left']} दिन बाकी)।\n"
            f"कृपया समय से पहले इसका नवीनीकरण (renewal) कराएँ और नया दस्तावेज़ अपलोड करें, ताकि आपका टर्मिनल चालू रहे।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\n"
            f"Your {c['doc_label_en']} expires on {c['expiry']} ({c['days_left']} days left).\n"
            f"Please renew it in time and upload the new document so your terminal stays active.")


def _renewal_followup(c):
    s, hi, en = _renewal_notice(c)
    return ("Reminder: " + s,
            "रिमाइंडर: " + hi + "\nअभी तक नया दस्तावेज़ नहीं मिला है।",
            "Reminder: " + en + "\nWe have not received the renewed document yet.")


def _renewal_final(c):
    return (f"URGENT: {c['doc_label_en']} expires in {c['days_left']} day(s) | KO {c['csp_code']}",
            f"ज़रूरी सूचना {c['csp_name']} (KO {c['csp_code']}):\n"
            f"आपका {c['doc_label_hi']} {c['days_left']} दिन में ({c['expiry']}) समाप्त हो जाएगा। "
            f"समाप्त होने पर आपका टर्मिनल बंद हो सकता है। कृपया तुरंत नया दस्तावेज़ अपलोड करें।",
            f"Urgent notice for {c['csp_name']} (KO {c['csp_code']}):\n"
            f"Your {c['doc_label_en']} expires in {c['days_left']} day(s) ({c['expiry']}). "
            f"Your terminal may be blocked after expiry. Please upload the renewed document now.")


def _upload_missing(c):
    return (f"Documents needed: {_needed(c, 'en')} | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपके दस्तावेज़ों की स्थिति:\n{_doc_lines(c, 'hi')}\n"
            f"कृपया ये दस्तावेज़ अपलोड करें: {_needed(c, 'hi')}।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nYour document status:\n{_doc_lines(c, 'en')}\n"
            f"Please upload: {_needed(c, 'en')}.")


def _upload_expired(c):
    return (f"Expired documents: please upload renewed copies | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपके कुछ दस्तावेज़ समाप्त हो चुके हैं:\n{_doc_lines(c, 'hi')}\n"
            f"कृपया नवीनीकरण कराकर नए दस्तावेज़ अपलोड करें: {_needed(c, 'hi')}। "
            f"समाप्त दस्तावेज़ों के कारण आपका टर्मिनल बंद हो सकता है।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nSome of your documents have expired:\n{_doc_lines(c, 'en')}\n"
            f"Please renew and upload: {_needed(c, 'en')}. Your terminal may be blocked while documents are expired.")


def _onboard_all(c):
    return (f"Please upload your CSP documents | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nहमारे पास पिछले 2 वर्षों में आपका कोई दस्तावेज़ दर्ज नहीं है।\n"
            f"कृपया तीनों दस्तावेज़ अपलोड करें: सीएसपी एग्रीमेंट, पुलिस वेरिफिकेशन / चरित्र प्रमाण पत्र, और आईआईबीएफ प्रमाण पत्र।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nWe have no documents on record for you from the last 2 years.\n"
            f"Please upload all three: CSP Agreement, Police Verification / Character Certificate, and IIBF Certificate.")


def _unreadable_reupload(c):
    return (f"Document not readable: please upload a scanned PDF | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपका भेजा गया {c.get('doc_label_hi', 'दस्तावेज़')} पढ़ा नहीं जा सका "
            f"(फोटो सही से नहीं ली गई है या धुंधली है)। कृपया स्कैन की हुई PDF दोबारा अपलोड करें।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nThe {c.get('doc_label_en', 'document')} you sent could not be read "
            f"(the photo is unclear or blurry). Please upload a scanned PDF again.")


def _escalation(role):
    def build(c):
        who = c.get("rm_name") if role == "RM" else c.get("dc_name")
        return (f"[{role} action] KO {c['csp_code']} {c['csp_name']}: documents pending",
                f"{who or role} जी,\nKO {c['csp_code']} ({c['csp_name']}, मोबाइल {c.get('csp_phone') or 'उपलब्ध नहीं'}) ने "
                f"बार-बार याद दिलाने के बाद भी दस्तावेज़ अपलोड नहीं किए हैं।\n{_doc_lines(c, 'hi')}\n"
                f"कृपया CSP से संपर्क करके अपलोड करवाएँ।",
                f"Dear {who or role},\nKO {c['csp_code']} ({c['csp_name']}, mobile {c.get('csp_phone') or 'not available'}) has not "
                f"uploaded documents despite {c.get('attempt') or 'several'} reminders.\n{_doc_lines(c, 'en')}\n"
                f"Please contact the CSP and help them upload. Their upload link: {c.get('upload_link') or '-'}")
    return build


TEMPLATES = {
    "RENEWAL_NOTICE": _renewal_notice,
    "RENEWAL_FOLLOWUP": _renewal_followup,
    "RENEWAL_FINAL": _renewal_final,
    "UPLOAD_MISSING": _upload_missing,
    "UPLOAD_EXPIRED": _upload_expired,
    "ONBOARD_ALL": _onboard_all,
    "UNREADABLE_REUPLOAD": _unreadable_reupload,
    "ESCALATION_RM": _escalation("RM"),
    "ESCALATION_DC": _escalation("DC"),
}
CSP_FACING = {"RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL", "UPLOAD_MISSING",
              "UPLOAD_EXPIRED", "ONBOARD_ALL", "UNREADABLE_REUPLOAD"}


def render(key: str, channel: str, ctx: dict[str, Any]) -> dict[str, str]:
    if key not in TEMPLATES:
        raise KeyError(f"unknown template {key}")
    subject, hi, en = TEMPLATES[key](ctx)
    csp_facing = key in CSP_FACING
    hi_parts = [hi, _link(ctx, "hi") if csp_facing else "", _contact(ctx, "hi") if csp_facing else ""]
    en_parts = [en, _link(ctx, "en") if csp_facing else "", _contact(ctx, "en") if csp_facing else ""]
    if csp_facing and channel == "EMAIL":
        hi_parts.append(SCAN_RULES_HI)
        en_parts.append(SCAN_RULES_EN)
    elif csp_facing:
        hi_parts.append("कृपया स्कैन की हुई साफ़ PDF ही अपलोड करें।")
        en_parts.append("Please upload a clear scanned PDF.")
    hindi = "\n".join(p for p in hi_parts if p)
    english = "\n".join(p for p in en_parts if p)
    sep = "\n\n———\n\n" if channel == "EMAIL" else "\n\n"
    footer = "\n\n— Eko CSP Support / ईको सीएसपी सहायता" if channel == "EMAIL" else "\n— Eko"
    return {"subject": subject, "body": hindi + sep + english + footer}
