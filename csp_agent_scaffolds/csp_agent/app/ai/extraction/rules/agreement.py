"""
CSP Agreement rules.

Issue date, first match wins:
  1. e-stamp "Certificate Issue(d) Date : 13-Feb-2025 11:29 AM" on any page.
     OCR mangles the label ("Coitigate Issued Date +", "Cortifitate ..."),
     so only "issue(d) date" is required, with a certificate-like word before
     it or an e-stamp certificate number (IN-XX...) nearby.
  2. The date just below the "CUSTOMER SERVICE POINT AGREEMENT" heading,
     e.g. "On this day of, 20/04/2024 ("Effective Date")" or
     "On this day of, 13th day of February 2025", printed or handwritten.
  3. A labelled "Effective Date : <date>" anywhere.

Validity, first match wins:
  a. "valid for a period of three (3) years" (or Hindi) anywhere -> 3 years.
  b. A range "valid from X to Y" / "from X till Y" / "for the period X to Y"
     / "X से Y तक" -> expiry = Y.
  c. Validity not stated anywhere: default 2 years (AGREEMENT_DEFAULT_YEARS).
"""
import re
from datetime import date
from typing import Optional

from ....expiry_engine import AGREEMENT_DEFAULT_YEARS
from .dates import find_dates, dates_after, plausible_issue_date, normalize, FoundDate

ESTAMP_LABEL = re.compile(r"(?:c\w{0,4}[rt]\w{0,6}\s*)?issu\w{0,3}\s*(?:date|dt)\b", re.I)
ESTAMP_CERT_NO = re.compile(r"\bIN-[A-Z]{2}\d{6,}", re.I)
HEADING = re.compile(r"customer\s*service\s*point\s*agreement", re.I)
EFFECTIVE = re.compile(r"effective\s*date", re.I)
EFFECTIVE_LABEL = re.compile(r"effective\s*date\s*[:\-–]\s*", re.I)

# OCR-tolerant: "three (3) years", "three (3) yea.s", "per od oJ three (3)",
# "Three (3) Years", "3 years", Hindi "तीन वर्ष" / "3 वर्ष" / "तीन साल".
THREE_YEAR = [
    re.compile(r"(?:valid|va\w?id|period|per\s?od|remain|validity|duration)[^.\n]{0,40}?\b(?:three|thr[e3]{2}|3)\b\s*(?:\(\s*3\s*\))?\s*y\w{1,4}", re.I),
    re.compile(r"\(\s*3\s*\)\s*y[ea]\w{0,3}", re.I),
    re.compile(r"(?:तीन|3)\s*(?:वर्ष|वर्षों|साल)"),
    # OCR on a slanted heading often moves "years" to another line, leaving
    # "...shall remain valid for a period of three (3)". The clause is
    # unambiguous without the word, as long as it isn't "three months".
    re.compile(r"(?:remain\s+)?valid\s+for\s+(?:a\s+)?period\s+of\s+(?:three|thr[e3]{2}|3)\b\s*(?:\(\s*3\s*\))?"
               r"(?!\s*(?:\(\s*3\s*\)\s*)?months?)", re.I),
]
RANGE = [
    re.compile(r"(?:valid\s+)?(?:from|w\.?\s?e\.?\s?f\.?)\s*[:\-]?\s*(?P<a>[^\n]{6,30}?)\s*(?:to|till|until|upto|up\s+to)\s*(?P<b>[^\n]{6,30})", re.I),
    re.compile(r"(?:for\s+the\s+)?period\s+(?:of\s+)?(?P<a>[^\n]{6,30}?)\s*(?:to|till|until)\s*(?P<b>[^\n]{6,30})", re.I),
    re.compile(r"(?P<a>[^\n]{6,30}?)\s*से\s*(?P<b>[^\n]{6,30}?)\s*तक"),
]


def _estamp_date(text: str, today: Optional[date]) -> Optional[FoundDate]:
    for m in ESTAMP_LABEL.finditer(text):
        # e-stamp tables put the value after the certificate number, so
        # look a little further than usual.
        window = text[m.end(): m.end() + 160]
        near_estamp = bool(re.search(r"c\w{0,4}[rt]\w{0,6}", text[max(0, m.start() - 15): m.end()], re.I)) \
            or bool(ESTAMP_CERT_NO.search(text[max(0, m.start() - 200): m.end() + 200]))
        if not near_estamp:
            continue
        for f in find_dates(window):
            if plausible_issue_date(f.value, today):
                return FoundDate(m.end() + f.start, m.end() + f.end, f.value, f.raw)
    return None


def _heading_date(text: str, today: Optional[date]) -> Optional[FoundDate]:
    for m in HEADING.finditer(text):
        window = text[m.end(): m.end() + 450]
        # Prefer the date sitting right before ("Effective Date").
        eff = EFFECTIVE.search(window)
        candidates = [f for f in find_dates(window) if plausible_issue_date(f.value, today)]
        if eff:
            before = [f for f in candidates if f.end <= eff.start()]
            if before:
                f = before[-1]
                return FoundDate(m.end() + f.start, m.end() + f.end, f.value, f.raw)
        # No "Effective Date" marker: accept a date only in the first few
        # lines under the heading (dates further down are usually footers).
        head = "\n".join(window.splitlines()[:6])
        for f in candidates:
            if f.end <= len(head):
                return FoundDate(m.end() + f.start, m.end() + f.end, f.value, f.raw)
    return None


def has_three_year_clause(text: str) -> bool:
    flat = re.sub(r"\s+", " ", normalize(text))
    return any(p.search(flat) for p in THREE_YEAR)


def find_validity_range(text: str, today: Optional[date] = None) -> Optional[tuple[date, date]]:
    """(start, end) of an explicit validity range between 30 days and 6 years."""
    flat = re.sub(r"[ \t]+", " ", normalize(text))
    for pat in RANGE:
        for m in pat.finditer(flat):
            a = find_dates(m.group("a"))
            b = find_dates(m.group("b"))
            if not a or not b:
                continue
            start, end = a[-1].value, b[0].value
            if start.year >= 2010 and 30 <= (end - start).days <= 6 * 366:
                return start, end
    return None


def extract_agreement(text: str, today: Optional[date] = None) -> dict:
    text = normalize(text)
    out = {"issue_date": None, "date_source": None, "explicit_expiry": None,
           "validity_years": None, "has_explicit_3year_clause": False, "validity_source": None}

    for source, fn in (("AGREEMENT_ESTAMP_CERT_ISSUE_DATE", _estamp_date),
                       ("AGREEMENT_HEADING_DATE", _heading_date)):
        found = fn(text, today)
        if found:
            out["issue_date"], out["date_source"] = found.value, source
            break
    if out["issue_date"] is None:
        labelled = dates_after(text, EFFECTIVE_LABEL, 40, today)
        if labelled:
            out["issue_date"], out["date_source"] = labelled[0].value, "AGREEMENT_EFFECTIVE_DATE_LABEL"

    if has_three_year_clause(text):
        out["has_explicit_3year_clause"] = True
        out["validity_years"] = 3
        out["validity_source"] = "AGREEMENT_EXPLICIT_3_YEAR"
    else:
        rng = find_validity_range(text, today)
        if rng:
            out["explicit_expiry"] = rng[1]
            out["validity_source"] = "AGREEMENT_RANGE_FROM_TO"
            if out["issue_date"] is None:
                out["issue_date"], out["date_source"] = rng[0], "AGREEMENT_RANGE_START"
        else:
            out["validity_years"] = AGREEMENT_DEFAULT_YEARS
            out["validity_source"] = "AGREEMENT_DEFAULT_2_YEAR"
    return out
