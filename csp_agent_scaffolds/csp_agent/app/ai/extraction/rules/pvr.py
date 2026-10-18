"""
Police Verification (PVR) / Character Certificate rules.

Issue date:
  1. The digital-signature date, e.g.
     "Digitally signed by KESHAV KUMAR  Date: 2026.01.28 18:29:17 +05'30'".
  2. Otherwise the date at the top of the certificate, e.g.
     "Application No. - 2025... Date - 07-05-2025 CHARACTER CERTIFICATE",
     "Date- 29/05/2025", "दिनांक 07/05/2025", "Approval Date: 10/07/2025".
  3. Otherwise the first plausible date in the document.

Validity:
  - "valid for six months" / "6 months" / "छह माह" / "6 माह" -> 6 months
  - "valid only for one year" / "1 year" / "एक वर्ष"          -> 12 months
  - "valid upto/till <date>"                                 -> that date
  - Bihar's online character certificate (reference "BCHC/2026/337487"):
    its fixed wording "इसकी वैधता निर्गत होने की तिथि से छः माह तक ही है"
    says 6 months, so 6 months even when OCR loses the Hindi paragraph.
  - otherwise the standard 12 months.
"""
import re
from datetime import date
from typing import Optional

from .dates import find_dates, dates_after, plausible_issue_date, normalize

SIGNATURE = re.compile(r"digitally\s*sign\w*\s*by", re.I)
SIGN_DATE_LABEL = re.compile(r"\bdate\s*[:\-]?\s*", re.I)
TOP_LABEL = re.compile(r"(?:approval\s*date|issue\s*date|date\s*of\s*issue|dated|date|दिनांक|तिथि|जारी\s*दिनांक|निर्गत\s*तिथि)\s*[:\-–.]*\s*", re.I)
VALID_UPTO = re.compile(r"(?:valid\s*(?:up\s*to|upto|till|until)|वैध\s*(?:तिथि)?)\s*[:\-]?\s*", re.I)

SIX_MONTHS = re.compile(r"(?:\b(?:six|6)\s*\(?\s*6?\s*\)?\s*months?\b|(?:छह|छः|छ:|6)\s*(?:माह|महीने|महीना))", re.I)
ONE_YEAR = re.compile(r"(?:\b(?:one|1)\s*(?:\(\s*1\s*\)\s*)?years?\b|(?:एक|1)\s*(?:वर्ष|साल))", re.I)
# "from the date of issue": Jharkhand "जारी होने की तिथि से केवल छह महीने के लिए वैध"
# where OCR often loses the last word.
# Bihar: "इसकी वैधता निर्गत होने की तिथि से छः माह तक ही है".
VALIDITY_CONTEXT = re.compile(r"valid|validity|वैध|मान्य|(?:जारी|निर्गत)\s*होने\s*की\s*तिथि\s*से|from\s+the\s+date\s+of\s+issue", re.I)
# Bihar Police online character certificate: "Appl. Ref. No : BCHC/2026/337487".
BIHAR_BCHC = re.compile(r"\bBCHC\s*/\s*20\d\d\s*/\s*\d{3,}", re.I)
# Police Clearance Certificate: it states no validity and is accepted for life.
PCC = re.compile(r"police\s*clearance\s*certificate|\bpcc\b|पुलिस\s*क्लीयरेंस|पुलिस\s*क्लियरेंस", re.I)


def _validity_months(text: str) -> tuple[Optional[int], Optional[str]]:
    flat = re.sub(r"\s+", " ", text)
    for m in VALIDITY_CONTEXT.finditer(flat):
        window = flat[max(0, m.start() - 40): m.end() + 60]
        if SIX_MONTHS.search(window):
            return 6, "PVR_STATED_6_MONTHS"
        if ONE_YEAR.search(window):
            return 12, "PVR_STATED_1_YEAR"
    return None, None


def _full_day(found: date, text: str, today: Optional[date]) -> date:
    """Tesseract's Hindi model sometimes drops a day's first digit ("3-09-2026"
    for "13-09-2026"). If the page's other reading has a plausible date in the
    same month and year whose day ends in this one, that is the date."""
    if found.day > 9:
        return found
    for f in find_dates(text):
        v = f.value
        if (v.year, v.month) == (found.year, found.month) and v.day > 9 and v.day % 10 == found.day \
                and plausible_issue_date(v, today):
            return v
    return found


def extract_pvr(text: str, today: Optional[date] = None) -> dict:
    text = normalize(text)
    out = {"issue_date": None, "date_source": None, "explicit_expiry": None,
           "validity_months": 12, "validity_source": "PVR_DEFAULT_1_YEAR"}

    for m in SIGNATURE.finditer(text):
        window = text[m.end(): m.end() + 200]
        label = SIGN_DATE_LABEL.search(window)
        search_in = window[label.end():] if label else window
        for f in find_dates(search_in):
            if plausible_issue_date(f.value, today):
                out["issue_date"], out["date_source"] = f.value, "PVR_DIGITAL_SIGNATURE_DATE"
                break
        if out["issue_date"]:
            break

    if out["issue_date"] is None:
        top = text[: max(800, len(text) // 4)]
        labelled = dates_after(top, TOP_LABEL, 25, today)
        if labelled:
            out["issue_date"], out["date_source"] = labelled[0].value, "PVR_TOP_DATE"
    if out["issue_date"] is None:
        for f in find_dates(text):
            if plausible_issue_date(f.value, today):
                out["issue_date"], out["date_source"] = f.value, "PVR_FIRST_DATE"
                break

    if out["date_source"] in ("PVR_TOP_DATE", "PVR_FIRST_DATE"):
        out["issue_date"] = _full_day(out["issue_date"], text, today)

    upto = [f for f in dates_after(text, VALID_UPTO, 30, today=date.max) if f.value.year >= 2010]
    if upto:
        out["explicit_expiry"] = upto[0].value
        out["validity_source"] = "PVR_PRINTED_EXPIRY"
        out["validity_months"] = None
    else:
        months, source = _validity_months(text)
        if months:
            out["validity_months"], out["validity_source"] = months, source
        elif BIHAR_BCHC.search(text):
            out["validity_months"], out["validity_source"] = 6, "PVR_STATED_6_MONTHS_BIHAR_BCHC"
        elif PCC.search(text):
            out["validity_months"], out["validity_source"] = None, "PVR_PCC_LIFETIME"
    return out
