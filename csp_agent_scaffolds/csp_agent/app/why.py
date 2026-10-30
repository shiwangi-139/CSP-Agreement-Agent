"""
app/why.py
The "why" of a document reading, in plain words, saved with the document
(extracted_fields["why"]) and shown in the Review queue and on the CSP's
page: which date was used and where it came from, which validity rule
applied, the exact text each came from, and what was ignored. Built from the
reader's own result (and its text), so it says what happened, not a guess.

    {"summary": "CSP Agreement, issued 15-06-2025, valid 2 years (stated in it) -> till 15-06-2027.",
     "lines": [{"what": "Issue date", "value": "15-06-2025", "how": "the date under the heading",
                "text": "On this day of 15.06.2025,(\\"Effective Date\\") CSP Name ..."}, ...],
     "ignored": ["Phone camera stamp: Oct 24, 2026, 17:13"]}
"""
import re
from datetime import date
from typing import Optional

from .ai.extraction.rules.dates import camera_stamp_lines

DOC = {"AGREEMENT": "CSP Agreement", "POLICE_VERIFICATION": "Police Verification", "IIBF_CERTIFICATE": "IIBF Certificate"}

DATE_FROM = {
    "AGREEMENT_ESTAMP_CERT_ISSUE_DATE": "the e-stamp certificate's issue date",
    "AGREEMENT_HEADING_DATE": "the date under the agreement's heading",
    "AGREEMENT_THIS_DAY_OF": "\"On this day of …\"",
    "AGREEMENT_EFFECTIVE_DATE_LABEL": "the \"Effective Date\" label",
    "AGREEMENT_RANGE_START": "the start of the \"from … to …\" range",
    "AGREEMENT_COMMENCING_DATE": "the \"commencing from …\" date (start of the term)",
    "PVR_DIGITAL_SIGNATURE_DATE": "the police officer's digital signature",
    "PVR_TOP_DATE": "the date at the top of the certificate",
    "PVR_FIRST_DATE": "the first date on the certificate",
    "IIBF_DIGITAL_SIGNATURE_DATE": "the certificate's digital signature",
    "IIBF_DATED": "the \"Dated\" line", "IIBF_FIRST_DATE": "the first date on the certificate",
    "CSP_PORTAL_TYPED": "the date typed on the upload page (not found on the document)",
    "MANUAL_REVIEW": "entered by a person in the Review queue",
}
VALIDITY = {
    "AGREEMENT_RANGE_FROM_TO": "the last day printed in the agreement",
    "AGREEMENT_PRINTED_EXPIRY": "the expiry date printed in the agreement",
    "AGREEMENT_EXPLICIT_3_YEAR": "3 years, stated in the agreement",
    "AGREEMENT_DEFAULT_1_YEAR": "1 year by Eko's rule (no validity written in the agreement)",
    "AGREEMENT_DEFAULT_2_YEAR": "2 years by the old default rule (no validity written)",
    "PVR_PRINTED_EXPIRY": "the expiry date printed on the certificate",
    "PVR_STATED_1_YEAR": "1 year, stated on the certificate",
    "PVR_STATED_6_MONTHS": "6 months, stated on the certificate",
    "PVR_STATED_6_MONTHS_BIHAR_BCHC": "6 months, stated on the Bihar certificate",
    "PVR_DEFAULT_1_YEAR": "1 year by Eko's rule (no validity written on the certificate)",
    "PVR_PCC_LIFETIME": "no expiry (Police Clearance Certificate, set by a person)",
    "IIBF_LIFETIME_NO_EXPIRY": "no expiry (IIBF certificates do not expire)",
    "MANUAL_REVIEW_EXPIRY_DATE": "the expiry date entered by a person",
}
# Words that sit next to a validity statement, to find its text.
VALIDITY_WORDS = re.compile(r"valid|validity|va\w?id|period|upto|up\s+to|till|until|ending|ends?\s+on|expir|years?|months?|"
                            r"मान्य|वैध|तक", re.I)


def _fmt(v) -> str:
    if not v:
        return ""
    if v == "LIFETIME_NO_EXPIRY":
        return "no expiry"
    try:
        return date.fromisoformat(str(v)[:10]).strftime("%d-%m-%Y")
    except ValueError:
        return str(v)


def _date_forms(iso: Optional[str]) -> list[str]:
    """Ways a date may be printed, to find it in the text."""
    try:
        d = date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return []
    forms = []
    for sep in ("/", "-", "."):
        forms += [f"{d.day:02d}{sep}{d.month:02d}{sep}{d.year}", f"{d.day}{sep}{d.month}{sep}{d.year}",
                  f"{d.year}{sep}{d.month:02d}{sep}{d.day:02d}", f"{d.day:02d}{sep}{d.month:02d}{sep}{str(d.year)[2:]}"]
    forms += [d.strftime("%d %B %Y"), d.strftime("%d %b %Y"), f"{d.day} {d.strftime('%B')}", d.strftime("%B %Y")]
    return forms


def _around(text: str, pos: int, end: int, width: int = 70) -> str:
    a, b = max(0, pos - width), min(len(text), end + width)
    s = re.sub(r"\s+", " ", text[a:b]).strip()
    return ("… " if a > 0 else "") + s + (" …" if b < len(text) else "")


def _find_date(text: str, iso: Optional[str]) -> Optional[str]:
    low = text.lower()
    for f in _date_forms(iso):
        i = low.find(f.lower())
        if i >= 0:
            return _around(text, i, i + len(f))
    return None


def _find_validity(text: str, end_iso: Optional[str]) -> Optional[str]:
    snip = _find_date(text, end_iso) if end_iso and end_iso != "LIFETIME_NO_EXPIRY" else None
    if snip:
        return snip
    for m in VALIDITY_WORDS.finditer(text):
        window = text[max(0, m.start() - 60): m.end() + 60]
        if re.search(r"\d|one|two|three|four|five|six|twelve|thirty|तीन|छः|छह|एक", window, re.I):
            return _around(text, m.start(), m.end(), 60)
    return None


def explain(ex: dict, typed_issue: Optional[date] = None) -> dict:
    text = ex.get("match_text") or ""
    t = ex.get("document_type")
    lines, ignored = [], []
    issue, end = ex.get("start_date"), ex.get("expiry_date")
    if issue:
        lines.append({"what": "Issue date", "value": _fmt(issue),
                      "how": DATE_FROM.get(ex.get("date_source") or "", (ex.get("date_source") or "").replace("_", " ").lower()),
                      "text": _find_date(text, issue)})
    rule = ex.get("validity_rule_used") or ""
    if end or rule:
        lines.append({"what": "Valid till", "value": _fmt(end) or "?",
                      "how": (f"{rule.split('_')[2].lower()} year by Eko's rule meanwhile: the validity sentence is "
                              "there but its number is hidden under a stamp (a person checks it)")
                      if ex.get("validity_hidden") and rule.startswith("AGREEMENT_DEFAULT")
                      else VALIDITY.get(rule, rule.replace("_", " ").lower()),
                      "text": None if "DEFAULT" in rule or "LIFETIME" in rule else _find_validity(text, end)})
    for line in camera_stamp_lines(text):
        ignored.append(f"Phone camera stamp: {line.strip()[:60]}")
    if typed_issue and issue and str(typed_issue) != str(issue)[:10]:
        ignored.append(f"Typed issue date {typed_issue:%d-%m-%Y} differs from the document; the document's date was used")
    pages = ex.get("page_count")
    if t == "AGREEMENT" and pages and pages < 10:
        ignored.append(f"{pages} page(s) in the file (a full agreement has about 13)")
    if ex.get("type_review"):
        ignored.append(f"Sent to a person: {ex['type_review'][:200]}")
    name = DOC.get(t, (t or "Document").replace("_", " ").title())
    if issue:
        val = next((l for l in lines if l["what"] == "Valid till"), None)
        summary = (f"{name}, issued {_fmt(issue)}" + (f", valid till {val['value']} ({val['how']})" if val else "") + ".")
    else:
        summary = f"{name}: no issue date could be read."
    return {"summary": summary, "lines": lines, "ignored": [i for i in ignored if i]}
