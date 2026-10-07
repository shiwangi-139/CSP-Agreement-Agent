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
    "• सारे पेज पूरे और सीधे हों, कोई हिस्सा कटा न हो।\n"
    "• अच्छी रोशनी में, धुंधला (blur) न हो। स्क्रीन की फोटो न लें।"
)
SCAN_RULES_EN = (
    "Upload rules:\n"
    "• Upload a scanned PDF (on your phone: Google Drive → Scan, the Adobe Scan app, or the camera's 'Document' mode).\n"
    "• All pages, complete and straight, nothing cut off.\n"
    "• Good light, not blurry. Do not photograph a screen."
)

STATUS_HI = {"VALID": "ठीक है ✅", "EXPIRED": "रिन्यू करना है (Expired)", "MISSING": "अभी जमा नहीं हुआ",
             "UNREADABLE": "फोटो साफ़ नहीं है"}
STATUS_EN = {"VALID": "Valid", "EXPIRED": "Expired", "MISSING": "Not received",
             "UNREADABLE": "Not readable (blurry)"}


def _doc_lines(ctx: dict, lang: str) -> str:
    lines = []
    for d in ctx.get("docs") or []:
        label = d["label_hi"] if lang == "hi" else d["label_en"]
        status = (STATUS_HI if lang == "hi" else STATUS_EN).get(d["status"], d["status"])
        extra = ""
        if d.get("expiry"):
            if lang != "hi":
                extra = f" (expiry: {d['expiry']})"
            elif d["status"] == "EXPIRED":
                extra = f" (तारीख निकल गई: {d['expiry']})"
            else:
                extra = f" (मान्य: {d['expiry']} तक)"
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
    return (f"मदद के लिए अपने RM {ctx['rm_name']}{phone} से संपर्क करें।" if lang == "hi"
            else f"For help, contact your RM {ctx['rm_name']}{phone}.")


# Documents a CSP gave to the RM earlier may never have reached us.
EARLIER_HI = "अगर आपने ये डॉक्यूमेंट पहले अपने RM को दिए थे, तो हो सकता है वो हमारे रिकॉर्ड में नहीं आए।"
EARLIER_EN = "If you gave them to your RM earlier, they may not have reached our records."
FORM_HI = "फॉर्म में आपकी कोई जानकारी खाली या गलत हो तो सही भर दें। हमारी टीम पूरी जाँच के बाद ही उसे बदलेगी।"
FORM_EN = "If any of your details in the form are missing or wrong, please correct them. Our team checks every change before it is applied."


def _link(ctx: dict, lang: str) -> str:
    if not ctx.get("upload_link"):
        return ""
    return (f"यहाँ अपलोड करें: {ctx['upload_link']}" if lang == "hi"
            else f"Upload here: {ctx['upload_link']}")


# Each builder returns (subject, hindi_block, english_block).
def _renewal_notice(c):
    return (f"Renewal due: {c['doc_label_en']} expires on {c['expiry']} | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\n"
            f"आपका {c['doc_label_hi']} {c['expiry']} तक ही मान्य है ({c['days_left']} दिन बाकी)।\n"
            f"कृपया समय रहते इसे रिन्यू करवा लें और नया डॉक्यूमेंट अपलोड करें, ताकि आपका काम बिना रुके चलता रहे।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\n"
            f"Your {c['doc_label_en']} expires on {c['expiry']} ({c['days_left']} days left).\n"
            f"Please renew it in time and upload the new document so your terminal stays active.")


def _renewal_followup(c):
    s, hi, en = _renewal_notice(c)
    return ("Reminder: " + s,
            "रिमाइंडर: " + hi + "\nहमें अभी तक नया डॉक्यूमेंट नहीं मिला है।",
            "Reminder: " + en + "\nWe have not received the renewed document yet.")


def _renewal_final(c):
    return (f"URGENT: {c['doc_label_en']} expires in {c['days_left']} day(s) | KO {c['csp_code']}",
            f"ज़रूरी: {c['csp_name']} (KO {c['csp_code']}):\n"
            f"आपका {c['doc_label_hi']} सिर्फ़ {c['days_left']} दिन और ({c['expiry']} तक) मान्य है। "
            f"इसके बाद आपका टर्मिनल रुक सकता है, इसलिए कृपया जल्दी से नया डॉक्यूमेंट अपलोड करें।",
            f"Urgent notice for {c['csp_name']} (KO {c['csp_code']}):\n"
            f"Your {c['doc_label_en']} expires in {c['days_left']} day(s) ({c['expiry']}). "
            f"Your terminal may be blocked after expiry. Please upload the renewed document now.")


def _upload_missing(c):
    return (f"Documents needed: {_needed(c, 'en')} | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपके डॉक्यूमेंट की जानकारी:\n{_doc_lines(c, 'hi')}\n"
            f"{EARLIER_HI}\nये डॉक्यूमेंट जमा करना ज़रूरी है, कृपया अपलोड करें: {_needed(c, 'hi')}।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nYour document status:\n{_doc_lines(c, 'en')}\n"
            f"{EARLIER_EN}\nThese documents are mandatory, please upload: {_needed(c, 'en')}.")


def _upload_expired(c):
    return (f"Expired documents: please upload renewed copies | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपके कुछ डॉक्यूमेंट की तारीख निकल चुकी है:\n{_doc_lines(c, 'hi')}\n"
            f"कृपया इन्हें रिन्यू करवाकर नए डॉक्यूमेंट अपलोड करें: {_needed(c, 'hi')}। "
            f"पुराने डॉक्यूमेंट की वजह से आपका टर्मिनल रुक सकता है।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nSome of your documents have expired:\n{_doc_lines(c, 'en')}\n"
            f"Please renew and upload: {_needed(c, 'en')}. Your terminal may be blocked while documents are expired.")


def _onboard_all(c):
    return (f"Please upload your CSP documents | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nहमारे रिकॉर्ड में अभी आपके सीएसपी डॉक्यूमेंट जमा नहीं हैं। {EARLIER_HI}\n"
            f"ये तीनों डॉक्यूमेंट जमा करना ज़रूरी है: सीएसपी एग्रीमेंट, पुलिस वेरिफिकेशन / चरित्र प्रमाण पत्र, और आईआईबीएफ सर्टिफिकेट।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nWe don't have your CSP documents on record yet. {EARLIER_EN}\n"
            f"All three are mandatory: CSP Agreement, Police Verification / Character Certificate, and IIBF Certificate.")


def _unreadable_reupload(c):
    return (f"Document not readable: please upload a scanned PDF | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपका {c.get('doc_label_hi', 'डॉक्यूमेंट')} जो हमें मिला था "
            f"(आपसे या आपके RM से), वो साफ़ पढ़ा नहीं जा सका (फोटो धुंधली है या पूरी नहीं है), इसलिए वो हमारे रिकॉर्ड में नहीं आ पाया।\n"
            f"ये डॉक्यूमेंट जमा करना ज़रूरी है। कृपया स्कैन की हुई साफ़ PDF फिर से अपलोड करें।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nThe {c.get('doc_label_en', 'document')} we received "
            f"(from you or your RM) could not be read (the photo is blurry or incomplete), so it is not on our records.\n"
            f"This document is mandatory. Please upload a clear scanned PDF again.")


def _link_request(c):
    # The CSP asked for their link on the public page (app/api/portal.py).
    return (f"Your document upload link | KO {c['csp_code']}",
            f"नमस्ते {c['csp_name']} (KO {c['csp_code']}),\nआपने डॉक्यूमेंट अपलोड करने का लिंक माँगा था। "
            f"यह लिंक सिर्फ़ आपके लिए है, इसे किसी और को न भेजें।",
            f"Hello {c['csp_name']} (KO {c['csp_code']}),\nYou asked for your document upload link. "
            f"It is only for you, please don't share it.")


def _escalation(role):
    def build(c):
        who = c.get("rm_name") if role == "RM" else c.get("dc_name")
        return (f"[{role} action] KO {c['csp_code']} {c['csp_name']}: documents pending",
                f"{who or role} जी,\nKO {c['csp_code']} ({c['csp_name']}, मोबाइल {c.get('csp_phone') or 'नहीं है'}) ने "
                f"कई बार याद दिलाने के बाद भी डॉक्यूमेंट अपलोड नहीं किए हैं।\n{_doc_lines(c, 'hi')}\n"
                f"कृपया CSP से बात करके अपलोड करवा दें।",
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
    "LINK_REQUEST": _link_request,
    "ESCALATION_RM": _escalation("RM"),
    "ESCALATION_DC": _escalation("DC"),
}
CSP_FACING = {"RENEWAL_NOTICE", "RENEWAL_FOLLOWUP", "RENEWAL_FINAL", "UPLOAD_MISSING",
              "UPLOAD_EXPIRED", "ONBOARD_ALL", "UNREADABLE_REUPLOAD", "LINK_REQUEST"}


def render(key: str, channel: str, ctx: dict[str, Any]) -> dict[str, str]:
    if key not in TEMPLATES:
        raise KeyError(f"unknown template {key}")
    subject, hi, en = TEMPLATES[key](ctx)
    csp_facing = key in CSP_FACING
    form = csp_facing and bool(ctx.get("upload_link"))
    hi_parts = [hi, _link(ctx, "hi") if csp_facing else "", FORM_HI if form else "",
                _contact(ctx, "hi") if csp_facing else ""]
    en_parts = [en, _link(ctx, "en") if csp_facing else "", FORM_EN if form else "",
                _contact(ctx, "en") if csp_facing else ""]
    if csp_facing and channel == "EMAIL":
        hi_parts.append(SCAN_RULES_HI)
        en_parts.append(SCAN_RULES_EN)
    elif csp_facing:
        hi_parts.append("कृपया स्कैन की हुई साफ़ PDF ही अपलोड करें।")
        en_parts.append("Please upload a clear scanned PDF.")
    hindi = "\n".join(p for p in hi_parts if p)
    english = "\n".join(p for p in en_parts if p)
    sep = "\n\n———\n\n" if channel == "EMAIL" else "\n\n"
    footer = "\n\n— Eko CSP Support / ईको सीएसपी हेल्प" if channel == "EMAIL" else "\n— Eko"
    return {"subject": subject, "body": hindi + sep + english + footer}
