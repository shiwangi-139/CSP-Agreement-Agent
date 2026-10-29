"""
app/safety_gate.py
The send-time safety gate (feedback loop, part C): a reading that cannot be
right is not accepted, so the CSP is never told "accepted" (or "expired")
on wrong dates. The document goes to the Review queue instead ("Safety
check: ..."), the CSP is told our team is checking it, and a person accepts,
corrects or rejects it as usual. Until then it doesn't count (it can't
replace a good copy on file) and no reminder asks for it.

Held (the same checks as the warning sign S4 in app/signals.py):
  expiry_before_issue   the expiry date is before the issue date
  issue_after_arrival   issued after the document reached us (+1 day)
  over_6_years          valid for more than 6 years
  disagrees_with_copy   another copy of the CSP's document has the same issue
                        date but another expiry date

Only new documents pass here (app/document_service.py); nothing stored is
changed.
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import canonical_type
from .models import Document, DocumentStatus

PREFIX = "Safety check: "
REFUSED = (DocumentStatus.REJECTED, DocumentStatus.UNREADABLE)


def _fmt(d) -> str:
    return d.strftime("%d-%m-%Y") if d else "?"


def impossible(issue: Optional[date], expiry: Optional[date], arrived: date) -> list[tuple[str, str]]:
    """(code, text) for each date that cannot be right."""
    out = []
    if issue and expiry and expiry < issue:
        out.append(("expiry_before_issue", f"expiry {_fmt(expiry)} is before issue {_fmt(issue)}"))
    if issue and issue > arrived + timedelta(days=1):
        out.append(("issue_after_arrival", f"issue date {_fmt(issue)} is after it arrived ({_fmt(arrived)})"))
    if issue and expiry and (expiry - issue).days > 6 * 366:
        out.append(("over_6_years", f"valid for over 6 years ({_fmt(issue)} → {_fmt(expiry)})"))
    return out


def check(db: Session, csp_id: int, doc_type: str, issue: Optional[date], expiry: Optional[date],
          arrived: date) -> Optional[str]:
    """The Review queue reason when this reading must not be accepted, else None."""
    problems = [text for _, text in impossible(issue, expiry, arrived)]
    if issue and expiry:
        for other in db.query(Document).filter(Document.csp_id == csp_id, Document.issue_date == issue,
                                               Document.expiry_date.isnot(None), Document.expiry_date != expiry,
                                               Document.readability == "READABLE", Document.status.notin_(REFUSED)):
            if canonical_type(other.document_type) == doc_type:
                problems.append(f"another copy (#{other.id}) with the same issue date was read as valid till "
                                f"{_fmt(other.expiry_date)}, this one till {_fmt(expiry)}")
                break
    if not problems:
        return None
    return (PREFIX + "; ".join(problems) + ". The CSP was not told it is accepted: check the dates on the "
            "document and accept, correct or reject.")
