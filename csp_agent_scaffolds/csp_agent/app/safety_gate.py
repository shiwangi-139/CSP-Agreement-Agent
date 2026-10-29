"""
app/safety_gate.py
The safety gate (harness): every new reading gets a risk score from its
evidence pack (app/evidence.py) before the CSP is told anything.

  hold   (score >= RISK_HOLD_AT, 60)   not accepted: Review queue
         ("Safety check: ..."), the CSP is told our team is checking it,
         and it doesn't count (it can't replace a good copy) until a person
         accepts, corrects or rejects it; no reminder asks for it meanwhile
  check  (score >= RISK_CHECK_AT, 30)  counts as read, and a person looks
         at it ("Risk check: ...")
  low    accepted as before (random spot checks still apply)

Points (each reason once):
  100  impossible dates: expiry before issue, issued after it reached us,
       valid over 6 years (the same checks as warning sign S4)
  100  a rule that read it is on probation (app/rule_health.py)
   70  another copy of this CSP's document has the same issue date but
       another expiry
   35  the 1-year default was used but an end date is printed on it
   15  another issue / signing / effective date on it, over a month away
   15  a rule that read it is on watch
   15  a person corrected this CSP's dates for this document before
   10  some pages could not be read, or the reading confidence is under 60 %
   10  the expiry is within 7 days of today (one day decides the status)

Only new documents pass here (app/document_service.py); nothing stored is
changed.
"""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import canonical_type
from .models import Document, DocumentStatus

PREFIX = "Safety check: "
CHECK_PREFIX = "Risk check: "
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


def _disagreeing_copy(db: Session, csp_id: int, doc_type: str, issue, expiry) -> Optional[Document]:
    if not (issue and expiry):
        return None
    for other in db.query(Document).filter(Document.csp_id == csp_id, Document.issue_date == issue,
                                           Document.expiry_date.isnot(None), Document.expiry_date != expiry,
                                           Document.readability == "READABLE", Document.status.notin_(REFUSED)):
        if canonical_type(other.document_type) == doc_type:
            return other
    return None


def assess(db: Session, csp_id: int, doc_type: str, ex: dict, issue: Optional[date], expiry: Optional[date],
           arrived: date, today: Optional[date] = None) -> dict:
    """{"score", "level", "reasons": [{"code", "points", "text"}], "evidence"}."""
    from .config import RISK_CHECK_AT, RISK_HOLD_AT
    from .evidence import build
    from .rule_health import held_rule, watched
    today = today or date.today()
    ev = build(db, csp_id, doc_type, ex, issue, expiry, arrived)
    reasons = []

    def add(code, points, text):
        reasons.append({"code": code, "points": points, "text": text})

    for code, text in impossible(issue, expiry, arrived):
        add(code, 100, text)
    rules = [ex.get("date_source"), ex.get("validity_rule_used")]
    h = held_rule(db, rules)
    if h is not None:
        add("rule_on_probation", 100, f"the rule '{h.rule}' is on probation ({h.state_reason or 'reads wrongly'})")
    other = _disagreeing_copy(db, csp_id, doc_type, issue, expiry)
    if other is not None:
        add("disagrees_with_copy", 70, f"another copy (#{other.id}) with the same issue date was read as valid till "
                                       f"{_fmt(other.expiry_date)}, this one till {_fmt(expiry)}")
    comp = ev["competing"]
    if comp["missed_end_matters"]:
        x = comp["missed_end"][0]
        add("missed_end_date", 35, f"the 1-year rule was used, but an end date {_fmt(date.fromisoformat(x['date']))} "
                                   f"is printed: \"{x['text']}\"")
    if comp["other_issue"]:
        x = comp["other_issue"][0]
        add("other_issue_date", 15, f"another {x['kind']} date {_fmt(date.fromisoformat(x['date']))} is printed: "
                                    f"\"{x['text']}\"")
    if h is None:
        w = watched(db, rules)
        if w:
            add("rule_on_watch", 15, f"the rule '{w[0].rule}' is on watch ({w[0].state_reason or ''})")
    if ev["csp"]["corrected_before"]:
        add("corrected_before", 15, "a person corrected this CSP's dates for this document before")
    doc = ev["document"]
    if (doc["pages"] and doc["pages_readable"] < doc["pages"]) or (doc["confidence"] is not None and doc["confidence"] < 0.6):
        add("weak_reading", 10, f"{doc['pages_readable']} of {doc['pages']} page(s) read, confidence {doc['confidence']}")
    if expiry and abs((expiry - today).days) <= 7:
        add("near_expiry", 10, f"the expiry {_fmt(expiry)} is within 7 days of today")
    score = sum(r["points"] for r in reasons)
    level = "hold" if score >= RISK_HOLD_AT else "check" if score >= RISK_CHECK_AT else "low"
    return {"score": score, "level": level, "reasons": reasons, "evidence": ev}


def queue_reason(risk: dict) -> str:
    """The Review queue reason for a held or checked reading."""
    why = "; ".join(r["text"] for r in sorted(risk["reasons"], key=lambda r: -r["points"]))
    if risk["level"] == "hold":
        return (f"{PREFIX}risk {risk['score']}: {why}. The CSP was not told it is accepted: check the dates on the "
                "document and accept, correct or reject.")
    return f"{CHECK_PREFIX}risk {risk['score']}: {why}. It counts meanwhile: check the dates and accept or correct."
