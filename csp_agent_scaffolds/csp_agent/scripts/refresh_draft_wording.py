"""
Bring the Hindi in unsent drafts up to the current everyday wording
(app/comms/templates.py): "डॉक्यूमेंट" instead of "दस्तावेज़", "मान्य … तक"
instead of "समाप्ति", "रिन्यू" instead of "नवीनीकरण", and so on. Drafts store
the finished text, so the old phrases are swapped in place; names, codes,
dates and each CSP's own link stay as they are.

    python -m scripts.refresh_draft_wording            # dry run: how many drafts would change
    python -m scripts.refresh_draft_wording --apply    # rewrite them

Sent messages are never touched.
"""
import argparse
import re

from app.db import SessionLocal
from app.models import OutboundMessage
from scripts.refresh_draft_links import UNSENT

# Order matters: whole sentences first, single words last.
PHRASES = [
    ("कृपया समय से पहले इसका नवीनीकरण (renewal) कराएँ और नया दस्तावेज़ अपलोड करें, ताकि आपका टर्मिनल चालू रहे।",
     "कृपया समय रहते इसे रिन्यू करवा लें और नया डॉक्यूमेंट अपलोड करें, ताकि आपका काम बिना रुके चलता रहे।"),
    ("\nअभी तक नया दस्तावेज़ नहीं मिला है।", "\nहमें अभी तक नया डॉक्यूमेंट नहीं मिला है।"),
    ("ज़रूरी सूचना ", "ज़रूरी: "),
    ("समाप्त होने पर आपका टर्मिनल बंद हो सकता है। कृपया तुरंत नया दस्तावेज़ अपलोड करें।",
     "इसके बाद आपका टर्मिनल रुक सकता है, इसलिए कृपया जल्दी से नया डॉक्यूमेंट अपलोड करें।"),
    ("आपके दस्तावेज़ों की स्थिति:", "आपके डॉक्यूमेंट की जानकारी:"),
    ("कृपया ये दस्तावेज़ अपलोड करें:", "कृपया ये डॉक्यूमेंट अपलोड करें:"),
    ("आपके कुछ दस्तावेज़ समाप्त हो चुके हैं:", "आपके कुछ डॉक्यूमेंट की तारीख निकल चुकी है:"),
    ("कृपया नवीनीकरण कराकर नए दस्तावेज़ अपलोड करें:", "कृपया इन्हें रिन्यू करवाकर नए डॉक्यूमेंट अपलोड करें:"),
    ("समाप्त दस्तावेज़ों के कारण आपका टर्मिनल बंद हो सकता है।", "पुराने डॉक्यूमेंट की वजह से आपका टर्मिनल रुक सकता है।"),
    ("हमारे पास पिछले 2 वर्षों में आपका कोई दस्तावेज़ दर्ज नहीं है।", "हमारे पास पिछले 2 साल का आपका कोई डॉक्यूमेंट नहीं है।"),
    ("कृपया तीनों दस्तावेज़ अपलोड करें:", "कृपया ये तीनों डॉक्यूमेंट अपलोड करें:"),
    ("आपका भेजा गया ", "आपका भेजा हुआ "),
    (" पढ़ा नहीं जा सका (फोटो सही से नहीं ली गई है या धुंधली है)। कृपया स्कैन की हुई PDF दोबारा अपलोड करें।",
     " हम पढ़ नहीं पाए (फोटो साफ़ नहीं है या धुंधली है)। कृपया स्कैन की हुई PDF फिर से अपलोड करें।"),
    ("उपलब्ध नहीं) ने ", "नहीं है) ने "),
    ("बार-बार याद दिलाने के बाद भी दस्तावेज़ अपलोड नहीं किए हैं।", "कई बार याद दिलाने के बाद भी डॉक्यूमेंट अपलोड नहीं किए हैं।"),
    ("कृपया CSP से संपर्क करके अपलोड करवाएँ।", "कृपया CSP से बात करके अपलोड करवा दें।"),
    ("सहायता के लिए अपने RM", "मदद के लिए अपने RM"),
    ("सभी पेज पूरे", "सारे पेज पूरे"),
    ("ईको सीएसपी सहायता", "ईको सीएसपी हेल्प"),
    ("समाप्त (Expired)", "रिन्यू करना है (Expired)"),
    ("पढ़ने योग्य नहीं (धुंधला)", "फोटो साफ़ नहीं है"),
    ("आईआईबीएफ प्रमाण पत्र", "आईआईबीएफ सर्टिफिकेट"),
]
PATTERNS = [
    (re.compile(r"को समाप्त हो रहा है \((\d+) दिन बाकी\)।"), r"तक ही मान्य है (\1 दिन बाकी)।"),
    (re.compile(r" (\d+) दिन में \(([^)\n]+)\) समाप्त हो जाएगा। "), r" सिर्फ़ \1 दिन और (\2 तक) मान्य है। "),
    # status lines "• label: status (समाप्ति: date)"
    (re.compile(r"^(• [^\n]*?): मान्य(?= \(|$)", re.M), r"\1: ठीक है ✅"),
    (re.compile(r"^(• [^\n]*?): प्राप्त नहीं$", re.M), r"\1: अभी जमा नहीं हुआ"),
    (re.compile(r"\(Expired\) \(समाप्ति: ([^)\n]+)\)"), r"(Expired) (तारीख निकल गई: \1)"),
    (re.compile(r"\(समाप्ति: ([^)\n]+)\)"), r"(मान्य: \1 तक)"),
]


def reword(text: str) -> str:
    for old, new in PHRASES:
        text = text.replace(old, new)
    for pat, new in PATTERNS:
        text = pat.sub(new, text)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    db = SessionLocal()
    try:
        changed, total = 0, 0
        for m in db.query(OutboundMessage).filter(OutboundMessage.status.in_(UNSENT)):
            total += 1
            p = dict(m.payload_json or {})
            new = {k: reword(v) for k, v in p.items() if k in ("body", "html") and isinstance(v, str)}
            if any(new[k] != p[k] for k in new):
                changed += 1
                if a.apply:
                    m.payload_json = {**p, **new}
        if a.apply:
            db.commit()
            print(f"reworded {changed} of {total} unsent drafts")
        else:
            print(f"dry run: {changed} of {total} unsent drafts would get the new wording. Run with --apply.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
