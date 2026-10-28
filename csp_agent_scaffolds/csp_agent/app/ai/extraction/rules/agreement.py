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
  0. A printed end date (30 days to 6 years after the issue date) -> expiry =
     that date. Wordings seen on real agreements: "shall be valid upto
     25/10/2027" (term clause), "... TO END AND CLOSE DATE 01-06-2028", also
     "valid till / until / up to", "Expiry Date:", "expires on", Hindi
     "<date> तक मान्य / वैध". A blank "valid upto ____" is not a date.
  a. "valid for a period of three (3) years" / "Full 3 year agreement" (or
     Hindi) anywhere -> 3 years.
  b. A range "valid from X to Y" / "from X till Y" / "for the period X to Y"
     / "X से Y तक" -> expiry = Y.
  c. "This Agreement ... valid for a period of two (2) / five years" or
     "... for a period of 36 months" -> that many years / months (three years
     is rule a).
  A printed last day that disagrees with a stated period by more than a
  month is reported in validity_conflict (a person checks it).
  d. Validity not stated anywhere: default 1 year (AGREEMENT_DEFAULT_YEARS).
     When the validity sentence is there but a stamp has wiped its number
     ("remain valid for a from the Effective Date"), validity_hidden is set so
     the person checking it is told to read the years in the heading.
"""
import re
from datetime import date
from typing import Optional

from ....expiry_engine import AGREEMENT_DEFAULT_YEARS, add_years
from .dates import find_dates, dates_after, plausible_issue_date, normalize, FoundDate

ESTAMP_LABEL = re.compile(r"(?:c\w{0,4}[rt]\w{0,6}\s*)?issu\w{0,3}\s*(?:date|dt)\b", re.I)
ESTAMP_CERT_NO = re.compile(r"\bIN-[A-Z]{2}\d{6,}", re.I)
HEADING = re.compile(r"customer\s*service\s*point\s*agreement", re.I)
EFFECTIVE = re.compile(r"effective\s*date", re.I)
EFFECTIVE_LABEL = re.compile(r"effective\s*date\s*[:\-–]\s*", re.I)
# Every Eko template opens "On this day of, 25/06/2025": found even when OCR
# garbles the heading (",USTOMER SERVICEINT AGREEMENT"). OCR also reads "day"
# as "gev" or "dav".
# The stamp over the first line can also swallow "On t": "nhis day of, 15-04-2026".
THIS_DAY_OF = re.compile(r"(?:on\s+th[il1]s\s+\w{2,4}|\b[tn]?h[il1]s\s+d[aeo][yv])\s+of\s*[,.:]?\s*[_\s]*", re.I)

# OCR-tolerant: "three (3) years", "three (3) yea.s", "per od oJ three (3)",
# "Three (3) Years", "3 years", Hindi "तीन वर्ष" / "3 वर्ष" / "तीन साल".
THREE_YEAR = [
    re.compile(r"(?:valid|va\w?id|period|per\s?od|remain|validity|duration)[^.\n]{0,40}?\b(?:three|thr[e3]{2}|3)\b\s*(?:\(\s*3\s*\))?\s*y\w{1,4}", re.I),
    re.compile(r"\(\s*3\s*\)\s*y[ea]\w{0,3}", re.I),
    re.compile(r"(?:तीन|3)\s*(?:वर्ष|वर्षों|साल)"),
    # "Full 3 year agreement", "three years agreement".
    re.compile(r"\b(?:three|thr[e3]{2}|3)\s*(?:\(\s*3\s*\)\s*)?y[ea]\w{0,3}'?s?\s+agreement", re.I),
    # OCR on a slanted heading often moves "years" to another line, leaving
    # "...shall remain valid for a period of three (3)". The clause is
    # unambiguous without the word, as long as it isn't "three months".
    re.compile(r"(?:remain\s+)?valid\s+for\s+(?:a\s+)?period\s+of\s+(?:three|thr[e3]{2}|3)\b\s*(?:\(\s*3\s*\))?"
               r"(?!\s*(?:\(\s*3\s*\)\s*)?months?)", re.I),
    # "...three years from the date of signing" (the word valid may be under a stamp)
    re.compile(r"\b(?:three|thr[e3]{2}|3)\s*(?:\(\s*3\s*\)\s*)?y\w{0,4}\s+from\s+the\s+date\s+of\s+(?:sign|exec)", re.I),
    # "valid" after the term, in a heading under the title: "3 Years For Valid",
    # "3 Years Valid", "Three (3) years validity" (Sunny Singh, 01-07-2026).
    re.compile(r"\b(?:three|thr[e3]{2}|3)\s*(?:\(\s*3\s*\)\s*)?y[ea]\w{0,3}'?s?\s*(?:for\s*)?va\w{0,2}d\w*", re.I),
]
# A raised "th" in "30th December 2023" often comes out of OCR as a quote
# mark: "30” December 2023", "29" December 2026".
_DATE = r"\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*(?:\d{4}|\d{2})(?!\d)|\d{1,2}(?:st|nd|rd|th|[\"”“'’`°]{1,2})?\s*[- ]?\s*[A-Za-z]{3,9}\.?\s*[- ,]?\s*\d{4}"
VALID_FROM = re.compile(r"\bvalid\w*\s*(?:from|w\.?\s?e\.?\s?f\.?)", re.I)
RANGE = [
    re.compile(r"(?:valid\s+)?(?:from|w\.?\s?e\.?\s?f\.?)\s*[:\-]?\s*(?P<a>[^\n]{6,30}?)\s*(?:to|till|until|upto|up\s+to)\s*(?P<b>[^\n]{6,30})", re.I),
    re.compile(r"(?:for\s+the\s+)?period\s+(?:of\s+)?(?P<a>[^\n]{6,30}?)\s*(?:to|till|until)\s*(?P<b>[^\n]{6,30})", re.I),
    re.compile(r"(?P<a>[^\n]{6,30}?)\s*से\s*(?P<b>[^\n]{6,30}?)\s*तक"),
    # "On this day of 23/10/2025 (Effective Date) to 22/10/2028" (Indrajeet Jha): the
    # label between the two dates hid the range.
    re.compile(rf"(?P<a>{_DATE})\s*[(\[]?\s*[\"“”']*\s*effective\s*date\s*[\"“”']*\s*[)\]]?\s*,?\s*"
               rf"(?:to|till|until|upto|up\s+to)\s*(?P<b>{_DATE})", re.I),
    # Two full dates joined by to/till/a dash, anywhere: "On this day of,
    # 26/03/2025 to 25/03/2027 (Effective Date)", "valid for two years i.e.
    # 06-06-2024 to 05-06-2026", "20/12/23 - 19/12/2025", "24-08-2025to23-8-2028".
    re.compile(rf"(?P<a>{_DATE})\s*(?:to|till|until|upto|up\s+to|-|–|—)\s*(?P<b>{_DATE})", re.I),
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


def _short_year_date(raw: str, end_year: int) -> Optional[date]:
    """'20/12/23' at the start of a range ending in 2025 is 20-12-2023: the
    other date gives the century. (A two-digit year alone is too ambiguous.)"""
    m = re.search(r"(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2})(?!\d)\s*$", raw.strip())
    if not m:
        return None
    year = (end_year // 100) * 100 + int(m.group(3))
    if year > end_year:
        year -= 100
    try:
        return date(year, int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


# A label right before the agreement's last day. Only separators may stand
# between the label and the date, so a blank "valid upto ____ or until
# terminated" never picks up a date from the next clause.
END_DATE = re.compile(
    r"\b(?:end\s*(?:and\s*close\s*)?date\s*(?:on\b)?"
    r"|valid(?:ity)?\s*(?:up\s*to|upto|till|until|thru|through)(?:\s*date)?"
    r"|expir(?:y|es|ing|ation)(?:\s*date)?(?:\s*on)?"
    # "commencing from 26/05/2025 an(d) ending on 25/05/2028", "ends on", "up to and including"
    # (a tick or stamp on the "n" of ending: OCR "e|ding", "e\ding", "eading" - Manoj Kumar Sah)
    r"|(?:\ba?n?d?\s*)?\be[a-z|\\/.,']{0,2}ding\s*(?:on|date)?|\bends?\s*on|up\s*to\s*and\s*including"
    # 'On this day of, 29/08/2025 ("Effective Date") 27/08/2028': the last day
    # written right after the bracketed "Effective Date" (the start date is
    # before it, and a date only 30 days to 6 years later can be the end).
    r"|effective\s*date\W{0,2}\s*\)"
    # 'On this day of, 11-06-2026, effected date 10-06-2029' (typed for "end date")
    r"|effected\s*date)\s*", re.I)
BETWEEN_LABEL_AND_DATE = re.compile(r"^[\s:\-–—_.,(]{0,12}$")
HINDI_UPTO = re.compile(r"\s*तक\s*(?:मान्य|वैध)")


def find_end_date(text: str, issue: Optional[date], today: Optional[date] = None) -> Optional[date]:
    """A printed last day ("valid upto 25/10/2027", "END AND CLOSE DATE
    01-06-2028", "Expiry Date: ...", "31/03/2027 तक मान्य", '("Effective
    Date") 27/08/2028') between 30 days
    and 6 years after the issue date (the scan may break the line before it).
    With no issue date read (only the term page is clear), a last day within
    6 years of today: the date the CSP typed is then the issue date."""
    flat = re.sub(r"\s+", " ", normalize(text))
    if issue is None:
        today = today or date.today()
        ok = lambda d: abs((d - today).days) <= 6 * 366
    else:
        ok = lambda d: 30 <= (d - issue).days <= 6 * 366
    for m in END_DATE.finditer(flat):
        window = flat[m.end():m.end() + 40]
        found = find_dates(window)
        if found and BETWEEN_LABEL_AND_DATE.match(window[:found[0].start]) and ok(found[0].value):
            return found[0].value
    for f in find_dates(flat):
        if HINDI_UPTO.match(flat, f.end) and ok(f.value):
            return f.value
    return None


YEAR_WORDS = {"one": 1, "two": 2, "four": 4, "five": 5, "1": 1, "2": 2, "4": 4, "5": 5}
# "This Agreement shall remain valid for a period of two (2) years": only in
# a sentence about the agreement, so "keep records for five years" can't count.
STATED_YEARS = re.compile(
    r"agreement[^.\n]{0,60}?(?:valid|validity|period|remain|term)[^.\n]{0,40}?"
    r"\b(one|two|four|five|[1245])\b\s*(?:\(\s*[1245]\s*\)\s*)?y(?:ea)?rs?\b", re.I)


# "This agreement would be Valid for Two Years from the date of signing" with a
# stamp over "Valid" (Shatrughna Ray): "N years from the date of signing" alone.
YEARS_FROM_SIGNING = re.compile(
    r"\b(one|two|four|five|[1245])\b\s*(?:\(\s*[1245]\s*\)\s*)?y(?:ea)?rs?\s+from\s+the\s+date\s+of\s+(?:sign|exec)", re.I)


# The heading "shall remain valid for a period of three (3) years from the
# Effective Date" with a notary seal over the middle: OCR keeps only "remain
# valid for a from the Effective Date" (Gurupad Mahato 1A852377). Nothing
# between "valid for" and "from the" may be a number, or it was read.
VALIDITY_HIDDEN = re.compile(
    r"va\w?id\s+for\s+(?:(?!\d|one\b|two\b|three|thr[e3]{2}|four|five|years?|months?)[^.\n]){0,30}?"
    r"from\s+the\s+(?:effective|date)", re.I)


def stated_years(text: str) -> Optional[int]:
    flat = re.sub(r"\s+", " ", normalize(text))
    m = STATED_YEARS.search(flat) or YEARS_FROM_SIGNING.search(flat)
    return YEAR_WORDS[m.group(1).lower()] if m else None


MONTH_WORDS = {"twelve": 12, "eighteen": 18, "twenty-four": 24, "twenty four": 24, "thirty-six": 36,
               "thirty six": 36, "forty-eight": 48, "forty eight": 48, "sixty": 60}
# "the terms of their relationship for a period of 36 months", "agreement ... thirty-six (36) months".
STATED_MONTHS = re.compile(
    r"(?:agreement|relationship|term)[^.\n]{0,80}?(?:period|for|valid|validity)[^.\n]{0,20}?"
    r"(?:of)?\s*(twelve|eighteen|twenty[- ]four|thirty[- ]six|forty[- ]eight|sixty|\d{2})\s*"
    r"(?:\(\s*\d{2}\s*\)\s*)?months?\b", re.I)


# "...the terms of their relationship. for a period of 36 months commencing
# from 16/01/2025": the sentence break hides "relationship" from the rule
# above; "period of N months commencing" is clear on its own.
PERIOD_COMMENCING = re.compile(
    r"\bfor\s+a\s+period\s+of\s+(twelve|eighteen|twenty[- ]four|thirty[- ]six|forty[- ]eight|sixty|\d{2})\s*"
    r"(?:\(\s*\d{2}\s*\)\s*)?months?\s+commenc", re.I)


def stated_months(text: str) -> Optional[int]:
    flat = re.sub(r"\s+", " ", normalize(text))
    m = STATED_MONTHS.search(flat) or PERIOD_COMMENCING.search(flat)
    if not m:
        return None
    raw = m.group(1).lower()
    n = MONTH_WORDS.get(raw) or (int(raw) if raw.isdigit() else None)
    return n if n and 12 <= n <= 72 else None


def stated_period_end(text: str, issue: date) -> Optional[date]:
    """The last day implied by a stated period ("2 years", "36 months"), if any."""
    from .dates import add_months
    if has_three_year_clause(text):
        return add_years(issue, 3)
    years = stated_years(text)
    if years:
        return add_years(issue, years)
    months = stated_months(text)
    return add_months(issue, months) if months else None


def find_validity_range(text: str, today: Optional[date] = None) -> Optional[tuple[date, date]]:
    """(start, end) of an explicit validity range between 30 days and 6 years."""
    flat = re.sub(r"[ \t]+", " ", normalize(text))
    for pat in RANGE:
        for m in pat.finditer(flat):
            a = find_dates(m.group("a"))
            b = find_dates(m.group("b"))
            if not b:
                continue
            start = a[-1].value if a else _short_year_date(m.group("a"), b[0].value.year)
            if start is None:
                continue
            end = b[0].value
            if start.year >= 2010 and 30 <= (end - start).days <= 6 * 366:
                return start, end
    # "This agreement is Valid from ____ 12/07/2025 ____ to ____ 11/07/2028"
    # with the stamp over it: OCR scatters the line ("Valid from\n12/07/2025\n
    # 1A85\nto 11/07/2028", or loses "to"). The first two dates within a few
    # lines after "valid from" are the range.
    for m in VALID_FROM.finditer(flat):
        found = find_dates(flat[m.end(): m.end() + 120])
        if len(found) >= 2:
            start, end = found[0].value, found[1].value
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
        after = dates_after(text, THIS_DAY_OF, 15, today)
        if after:
            out["issue_date"], out["date_source"] = after[0].value, "AGREEMENT_THIS_DAY_OF"
    if out["issue_date"] is None:
        labelled = dates_after(text, EFFECTIVE_LABEL, 40, today)
        if labelled:
            out["issue_date"], out["date_source"] = labelled[0].value, "AGREEMENT_EFFECTIVE_DATE_LABEL"

    end = find_end_date(text, out["issue_date"], today)
    if end and out["issue_date"] is None:
        # Only the last day is clear ("valid upto 03/12/2027" on the term page):
        # kept, with the issue date the CSP typed (a person confirms it).
        out["explicit_expiry"] = end
        out["validity_source"] = "AGREEMENT_RANGE_FROM_TO"
        return out
    if end:
        out["explicit_expiry"] = end
        out["validity_source"] = "AGREEMENT_RANGE_FROM_TO"
        implied = stated_period_end(text, out["issue_date"])
        if implied and abs((implied - end).days) > 31:
            out["validity_conflict"] = (f"The agreement prints {end:%d-%m-%Y} as its last day, but its stated period "
                                        f"ends {implied:%d-%m-%Y}: check which is right.")
    elif has_three_year_clause(text):
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
        elif out["issue_date"] and stated_months(text):
            out["explicit_expiry"] = stated_period_end(text, out["issue_date"])
            out["validity_source"] = "AGREEMENT_RANGE_FROM_TO"       # a known last day, like a range
        elif out["issue_date"] and stated_years(text):
            years = stated_years(text)
            out["explicit_expiry"] = add_years(out["issue_date"], years)
            out["validity_years"] = years
            out["validity_source"] = "AGREEMENT_RANGE_FROM_TO"       # a known last day, like a range
        else:
            out["validity_years"] = AGREEMENT_DEFAULT_YEARS
            out["validity_source"] = f"AGREEMENT_DEFAULT_{AGREEMENT_DEFAULT_YEARS}_YEAR"
            out["validity_hidden"] = bool(VALIDITY_HIDDEN.search(re.sub(r"\s+", " ", text)))
    return out
