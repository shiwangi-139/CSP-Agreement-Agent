"""
app/evidence.py
The evidence pack of one reading (context engineering): everything the
decision about a document is based on, in three layers, saved with the
document (extracted_fields["evidence"]) and used by the risk score
(app/safety_gate.py):

  document  every date printed on it, each with its kind (issue / signing /
            effective / digital signature / end / birth / camera stamp /
            other) from the words around it, the text, and whether it was
            used; how well it was read (OCR route, pages read, confidence)
  rules     the date rule and the validity rule used, and their health
            (app/rule_health.py)
  csp       the CSP's other copies of this document and earlier corrections
            by a person

Kinds come from the words just before a date (and "from ... to" ranges), so
a date the rules skipped is still visible, e.g. a printed end date that the
1-year default ignored. No full text is kept: only short snippets.
"""
import re
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from .ai.extraction.rules.dates import camera_stamp_lines, find_dates
from .compliance import canonical_type
from .models import Document, DocumentStatus, ExtractionCorrection

MAX_DATES = 25
KINDS = [  # (kind, words right before the date), first match wins
    ("birth", r"birth|d\.?\s?o\.?\s?b|जन्म"),
    ("digital_signature", r"digitally\s+signed|signature\s+valid|signed\s+by.{0,60}date|date\s*:\s*$"),
    ("end", r"valid\w*\s+(?:up\s*to|upto|till|until)|ending\s+on|expir\w*|\bto\s*$|\btill\s*$|until\s*$|upto\s*$|तक|समाप्त"),
    ("effective", r"effective|commenc\w*|w\.?e\.?f|with\s+effect|from\s*$|से\s*$"),
    ("signing", r"this\s+day\s+of|signed\s+on|executed|signature|place.{0,20}date|हस्ताक्षर"),
    ("issue", r"issue\w*|dated?|दिनांक|जारी|certificate\s+no|application\s+no"),
]
_KIND_RE = [(k, re.compile(p, re.I)) for k, p in KINDS]


def _kind(before: str) -> str:
    """From the words before the date on its own line (and the line above
    when the date starts its line)."""
    lines = before.split("\n")
    tail = lines[-1] if len(lines[-1].strip()) >= 12 or len(lines) == 1 else "\n".join(lines[-2:])
    tail = tail[-70:]
    for k, rx in _KIND_RE:
        if rx.search(tail):
            return k
    return "other"


def _snip(text: str, a: int, b: int, w: int = 50) -> str:
    return re.sub(r"\s+", " ", text[max(0, a - w): b + 25]).strip()[:140]


def _d(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def dates_on_document(text: str, issue: Optional[date], expiry: Optional[date]) -> list[dict]:
    """Every date printed, with its kind and whether the reading used it."""
    camera = " ".join(camera_stamp_lines(text))
    out = []
    for f in find_dates(text):
        before = text[max(0, f.start - 80): f.start]
        kind = "camera_stamp" if f.raw and f.raw in camera else _kind(before)
        used = "issue" if issue and f.value == issue else "expiry" if expiry and f.value == expiry else None
        out.append({"date": f.value.isoformat(), "kind": kind, "used": used, "text": _snip(text, f.start, f.end)})
        if len(out) >= MAX_DATES:
            break
    return out


def competing(dates: list[dict], issue: Optional[date], expiry: Optional[date], arrived: date,
              default_validity: bool) -> dict:
    """Dates that say something else than the reading:
    other_issue  an issue / signing / effective date more than 31 days from
                 the issue date used (and not after the document arrived)
    missed_end   an end date after the issue date, more than 31 days from the
                 expiry used (for a 1-year default: a printed end the rules missed)"""
    other_issue, missed_end = [], []
    for x in dates:
        v = _d(x["date"])
        if v is None or x["used"]:
            continue
        if issue and x["kind"] in ("issue", "signing", "effective") and abs((v - issue).days) > 31 and v <= arrived:
            other_issue.append(x)
        if issue and expiry and x["kind"] == "end" and v > issue and abs((v - expiry).days) > 31 \
                and (v - issue).days <= 6 * 366:
            missed_end.append(x)
    return {"other_issue": other_issue[:5], "missed_end": missed_end[:5],
            "missed_end_matters": bool(missed_end) and default_validity}


def csp_history(db: Session, csp_id: int, doc_type: str) -> dict:
    copies = [d for d in db.query(Document).filter(Document.csp_id == csp_id, Document.readability == "READABLE")
              if canonical_type(d.document_type) == doc_type]
    ids = [d.id for d in copies]
    corrected = db.query(ExtractionCorrection).filter(ExtractionCorrection.document_id.in_(ids),
                                                      ExtractionCorrection.field_name.in_(["issue_date", "expiry_date"])
                                                      ).count() if ids else 0
    corrected += sum(1 for d in copies if d.date_source == "MANUAL_REVIEW"
                     or (d.validity_rule_used or "").startswith("MANUAL_REVIEW"))
    return {"copies": len(copies),
            "accepted_copies": sum(1 for d in copies if d.status in (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED)),
            "refused_copies": sum(1 for d in copies if d.status == DocumentStatus.REJECTED),
            "corrected_before": corrected}


def build(db: Session, csp_id: int, doc_type: str, ex: dict, issue: Optional[date], expiry: Optional[date],
          arrived: date) -> dict:
    text = ex.get("match_text") or ""
    dates = dates_on_document(text, issue, expiry)
    default_validity = "DEFAULT" in (ex.get("validity_rule_used") or "")
    pages, readable = ex.get("page_count") or 0, ex.get("pages_readable") or 0
    return {
        "document": {"dates": dates, "ocr": ex.get("ocr_method"), "pages": pages, "pages_readable": readable,
                     "confidence": ex.get("confidence"), "camera_stamps": len(camera_stamp_lines(text))},
        "rules": {"date": ex.get("date_source"), "validity": ex.get("validity_rule_used")},
        "csp": csp_history(db, csp_id, doc_type),
        "competing": competing(dates, issue, expiry, arrived, default_validity),
    }
