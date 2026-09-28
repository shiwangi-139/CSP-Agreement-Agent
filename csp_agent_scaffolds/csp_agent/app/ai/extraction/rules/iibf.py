"""
IIBF BC/BF certificate rules. Lifetime validity; we record issue date,
holder name and membership/registration number.

The certificate body is usually an image, so these run on OCR text, e.g.
  "Membership No./ ... 193037 do hereby certify that DEEPAK SHAKYA
   CERTIFICATE EXAMINATION FOR BUSINESS CORRESPONDENTS / FACILITATORS ...
   MUMBAI, DATED 08th JUL 2022"
plus the text layer's "Digitally signed by DS INDIAN INSTITUTE OF BANKING
AND FINANCE 3 Date: 2022.07.29 10:47:17 IST".

Issue date: "DATED <date>" -> digital-signature date -> first plausible date.
"""
import re
from datetime import date
from typing import Optional

from .dates import find_dates, dates_after, plausible_issue_date, normalize

DATED = re.compile(r"\bdated\b\s*[:\-]?\s*", re.I)
SIGN_DATE = re.compile(r"digitally\s*sign\w*\s*by[\s\S]{0,120}?\bdate\s*[:\-]?\s*", re.I)
NAME_BEFORE_EXAM = re.compile(r"(?:certify\s+that|contify\s+that|certified\s+that)?\s*"
                              r"(?:shri|smt|mr|mrs|ms|kumari|km)?\.?\s*"
                              r"(?P<name>[A-Z][A-Z.]+(?:\s+[A-Z][A-Z.]+){0,4})\s+(?:has\s+passed\s+)?(?:the\s+)?CERTIFICATE\s+EXAMINATION")
NAME_AFTER_CERTIFY = re.compile(r"(?:certify|certified)\s+that\s+(?:shri|smt|mr|mrs|ms|kumari|km)?\.?\s*"
                                r"(?P<name>[A-Z][A-Za-z.]+(?:\s+[A-Z][A-Za-z.]+){0,4})")
MEMBERSHIP = re.compile(r"(?:membership|registration|regn?|enrol+ment|roll)\s*\w{0,3}\s*(?:no|number|num)?\.?\s*[/:#-]*\s*(?:[A-Za-z]{0,12}\s*[./:]?\s*){0,3}?(?P<num>\d{6,12})", re.I)
NOT_NAMES = {"INDIAN", "INSTITUTE", "BANKING", "FINANCE", "MUMBAI", "THE", "CERTIFICATE", "SIGNATURE"}


def _holder_name(text: str) -> Optional[str]:
    for pat in (NAME_BEFORE_EXAM, NAME_AFTER_CERTIFY):
        for m in pat.finditer(text):
            words = [w for w in m.group("name").split() if w.strip(".").upper() not in NOT_NAMES]
            if 1 <= len(words) <= 5 and sum(len(w) for w in words) >= 4:
                return " ".join(w.strip(".") for w in words).title()
    return None


def extract_iibf(text: str, today: Optional[date] = None) -> dict:
    text = normalize(text)
    out = {"issue_date": None, "date_source": None, "holder_name": _holder_name(text),
           "registration_number": None, "validity_source": "IIBF_LIFETIME_NO_EXPIRY"}

    for source, pat in (("IIBF_DATED", DATED), ("IIBF_DIGITAL_SIGNATURE_DATE", SIGN_DATE)):
        found = dates_after(text, pat, 30, today)
        if found:
            out["issue_date"], out["date_source"] = found[0].value, source
            break
    if out["issue_date"] is None:
        for f in find_dates(text):
            if plausible_issue_date(f.value, today):
                out["issue_date"], out["date_source"] = f.value, "IIBF_FIRST_DATE"
                break

    m = MEMBERSHIP.search(text)
    if m:
        out["registration_number"] = m.group("num")
    return out
