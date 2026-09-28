"""
Date finding for CSP documents, in English and Hindi, tolerant of OCR noise.

Handles, among others:
  20/04/2024  5/1/2024  2026.01.28  2024-05-12  07-05-2025
  13-Feb-2025 11:29 AM   04-Dec-2024   08th JUL 2022   23rd DEC 2022
  13" day of February 2025   February 13, 2025
  ०७/०५/२०२५ (Devanagari digits)   07 मई 2025 (Hindi month names)
"""
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

MIN_YEAR = 2010

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "जनवरी": 1, "फरवरी": 2, "फ़रवरी": 2, "मार्च": 3, "अप्रैल": 4, "अप्रेल": 4,
    "मई": 5, "जून": 6, "जुलाई": 7, "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9,
    "अक्टूबर": 10, "अक्तूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12, "दिसम्बर": 12,
}

_EN_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_HI_MON = "(" + "|".join(sorted((k for k in MONTHS if not k.isascii()), key=len, reverse=True)) + ")"
_DAY = r"(0?[1-9]|[12]\d|3[01])"
_YEAR = r"((?:19|20)\d{2})"
_ORD = r"(?:\s*(?:st|nd|rd|th|\"|”|'))?"

PATTERNS = [
    # 2026.01.28 / 2024-05-12 / 2024/05/12
    ("ymd", re.compile(r"(?<![\dA-Za-z])" + _YEAR + r"\s?[./-]\s?(0?[1-9]|1[0-2])\s?[./-]\s?" + _DAY + r"(?!\d)")),
    # 20/04/2024, 5/1/2024, 07-05-2025, 07.05.2025
    ("dmy", re.compile(r"(?<![\dA-Za-z])" + _DAY + r"\s?[./-]\s?(0?[1-9]|1[0-2])\s?[./-]\s?" + _YEAR + r"(?!\d)")),
    # 13-Feb-2025, 08th JUL 2022, 13" day of February 2025, 4 Dec, 2024
    ("d_mon_y", re.compile(r"(?<![\dA-Za-z])" + _DAY + _ORD + r"[\s./-]*(?:day\s+of\s+)?" + _EN_MON
                           + r"[a-z]*\.?[\s,./-]*" + _YEAR + r"(?!\d)", re.I)),
    # February 13, 2025 / Feb 13 2025
    ("mon_d_y", re.compile(r"\b" + _EN_MON + r"[a-z]*\.?\s+" + _DAY + _ORD + r",?\s+" + _YEAR + r"(?!\d)", re.I)),
    # 07 मई 2025
    ("d_himon_y", re.compile(r"(?<!\d)" + _DAY + r"\s*" + _HI_MON + r"[\s,]*" + _YEAR + r"(?!\d)")),
]


@dataclass
class FoundDate:
    start: int
    end: int
    value: date
    raw: str


_OCR_FIXES = [
    # Only unambiguous, same-length fixes, so text offsets stay meaningful.
    (re.compile(r"\b0ct\b", re.I), "Oct"),
    (re.compile(r"\bN0v\b", re.I), "Nov"),
    (re.compile(r"(?<=\d)[Oo](?=\d)"), "0"),
    (re.compile(r"(?<=\d)[lI|](?=\d)"), "1"),
]


def normalize(text: str) -> str:
    """Devanagari digits -> ASCII plus a few unambiguous OCR slips
    ("0ct" -> "Oct", "2O24" -> "2024"). A letter that could be one of
    several digits (e.g. "B7") is left alone: we never guess a date."""
    text = (text or "").translate(DEVANAGARI_DIGITS)
    for pat, rep in _OCR_FIXES:
        text = pat.sub(rep, text)
    return text


def _build(kind: str, g: tuple) -> Optional[date]:
    try:
        if kind == "ymd":
            y, m, d = int(g[0]), int(g[1]), int(g[2])
        elif kind == "dmy":
            d, m, y = int(g[0]), int(g[1]), int(g[2])
        elif kind == "d_mon_y":
            d, m, y = int(g[0]), MONTHS[g[1].lower()[:3]], int(g[2])
        elif kind == "mon_d_y":
            m, d, y = MONTHS[g[0].lower()[:3]], int(g[1]), int(g[2])
        elif kind == "d_himon_y":
            d, m, y = int(g[0]), MONTHS[g[1]], int(g[2])
        else:
            return None
        return date(y, m, d)
    except (ValueError, KeyError):
        return None


def find_dates(text: str) -> list[FoundDate]:
    """Every date in the text, in reading order, without overlaps."""
    text = normalize(text)
    found: list[FoundDate] = []
    for kind, pat in PATTERNS:
        for m in pat.finditer(text):
            value = _build(kind, m.groups())
            if value is None:
                continue
            if any(not (m.end() <= f.start or m.start() >= f.end) for f in found):
                continue
            found.append(FoundDate(m.start(), m.end(), value, m.group(0)))
    return sorted(found, key=lambda f: f.start)


def plausible_issue_date(d: date, today: Optional[date] = None) -> bool:
    """An issue date can't be in the future and isn't older than MIN_YEAR."""
    today = today or date.today()
    return MIN_YEAR <= d.year and d <= today


def first_date(text: str, today: Optional[date] = None, plausible: bool = True) -> Optional[FoundDate]:
    for f in find_dates(text):
        if not plausible or plausible_issue_date(f.value, today):
            return f
    return None


def dates_after(text: str, pattern: re.Pattern, window: int, today: Optional[date] = None) -> list[FoundDate]:
    """Plausible dates that start within `window` characters after each match
    of `pattern`, in order. Offsets are relative to `text`."""
    text = normalize(text)
    all_dates = find_dates(text)
    out = []
    for m in pattern.finditer(text):
        for f in all_dates:
            if m.end() <= f.start <= m.end() + window and plausible_issue_date(f.value, today):
                out.append(f)
                break
    return out


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, min(d.day, day))
        except ValueError:
            continue
    raise ValueError(f"cannot add {months} months to {d}")
