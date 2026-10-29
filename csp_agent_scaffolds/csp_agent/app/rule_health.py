"""
app/rule_health.py
Every reading rule's health, and the circuit breaker (harness).

A rule is a date source (where the issue date came from, e.g.
AGREEMENT_THIS_DAY_OF) or a validity rule (how the expiry was set, e.g.
AGREEMENT_EXPLICIT_3_YEAR). Every night (app/worker.py, 03:10, after the
02:30 reading check and the 03:00 warning signs) each rule gets:

  docs_30d          new documents it read in 30 days
  corrected_14d     a person changed the date it read (Review queue), 14 days
  eval_*            the latest nightly reading check (tuning set): checked,
                    correct, wrong accepts, wrong rejects, precision

and a state:
  probation  2+ wrong accepts in the reading check with precision under
             97 %, or precision under 90 %, or 3+ corrections in 14 days that are 15 %+ of what it read,
             or the release check after an update broke a document with it.
             New readings by the rule then wait for a person
             (app/safety_gate.py) until it is fixed (the next check is clean:
             released by itself) or a person releases it.
  watch      one correction, 2+ wrong rejects, or a single wrong accept
             (it may be a wrong answer in the set): only shown.
  ok

Policy defaults (the 1-year rule, "no expiry", dates typed by people) are
never put on probation: the Review queue already checks those.

Release check: when the reading check ran on new code (another commit) and
documents it read right before are now accepted wrongly, the rules that read
them go on probation at once (a soft rollback: nothing is undone in the
code, but their readings wait for a person), and the check is saved for the
Rules page and the Coach.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .compliance import canonical_type
from .models import (Document, IngestState, ManualReviewQueue, MonitorRun, ReviewStatus, RuleEvent, RuleHealth)

logger = logging.getLogger(__name__)

PRECISION_FLOOR = 0.97        # with 2+ wrong accepts
PRECISION_BREAK = 0.90        # with any wrong accept
CORRECTION_DAYS = 14
RELEASE_CHECK_KEY = "release_check"
# Never on probation (policy, or set by a person).
NEVER_HELD = ("DEFAULT", "MANUAL", "LIFETIME", "CSP_PORTAL_TYPED", "MODEL_VISION")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def can_hold(rule: Optional[str]) -> bool:
    return bool(rule) and not any(w in rule for w in NEVER_HELD)


def describe(rule: str) -> str:
    from .why import DATE_FROM, VALIDITY
    return DATE_FROM.get(rule) or VALIDITY.get(rule) or rule.replace("_", " ").lower()


def _kind(rule: str) -> str:
    from .why import DATE_FROM
    return "date" if rule in DATE_FROM or "DATE" in rule or "FIRST" in rule or "THIS_DAY" in rule else "validity"


def _type_of(rule: str) -> Optional[str]:
    for t in ("AGREEMENT", "PVR", "IIBF"):
        if rule.startswith(t):
            return {"PVR": "POLICE_VERIFICATION", "IIBF": "IIBF_CERTIFICATE"}.get(t, t)
    return None


def _event(db: Session, rule: str, event: str, detail: str, by: str = "agent", now: Optional[datetime] = None) -> None:
    db.add(RuleEvent(rule=rule, at=now or _now(), event=event, detail=(detail or "")[:1000], by=by))


def _latest_eval(db: Session, set_name: str = "tuning", n: int = 2) -> list[MonitorRun]:
    out = []
    for r in db.query(MonitorRun).filter(MonitorRun.kind == "reading_eval").order_by(MonitorRun.started_at.desc()).limit(30):
        if (r.summary or {}).get("set") == set_name:
            out.append(r)
            if len(out) == n:
                break
    return out


def _corrections(db: Session, since: datetime) -> dict:
    """rule -> [(resolved_at, document id)]: a person changed what the rule read.
    The rule is the one stored when the document was read (extracted_fields),
    since a correction overwrites the document's own rule columns."""
    out = defaultdict(list)
    rows = (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
            .filter(ManualReviewQueue.status == ReviewStatus.CORRECTED, ManualReviewQueue.resolved_at >= since))
    for q, d in rows:
        ex = d.extracted_fields or {}
        read_issue, read_expiry = str(ex.get("start_date") or "")[:10], str(ex.get("expiry_date") or "")[:10]
        if read_issue and d.issue_date and read_issue != d.issue_date.isoformat():
            rule = ex.get("date_source")
        elif read_expiry and d.expiry_date and read_expiry != d.expiry_date.isoformat():
            rule = ex.get("validity_rule_used")
        else:
            continue
        if rule:
            out[rule].append((q.resolved_at, d.id))
    return out


def release_check(db: Session, now: Optional[datetime] = None) -> Optional[dict]:
    """The reading check's latest run against the one before, when the code
    changed in between. Saved once per commit."""
    now = now or _now()
    runs = _latest_eval(db)
    if len(runs) < 2:
        return None
    new, old = (r.summary or {} for r in runs)
    if not new.get("commit") or new.get("commit") == old.get("commit"):
        return None
    st = db.get(IngestState, RELEASE_CHECK_KEY)
    if st is not None and (st.value or {}).get("commit") == new["commit"]:
        return st.value
    before, after = old.get("failures") or {}, new.get("failures") or {}
    broken = {k: v for k, v in after.items() if k not in before}
    fixed = [k for k in before if k not in after]
    rules = defaultdict(list)
    for doc_id, v in broken.items():
        if v == "WRONG ACCEPT":
            for rule in (new.get("failure_rules") or {}).get(doc_id, []):
                rules[rule].append(doc_id)
    out = {"commit": new["commit"], "previous_commit": old.get("commit"), "at": now.isoformat(timespec="minutes"),
           "fixed": len(fixed), "fixed_ids": fixed[:50],
           "broken_wrong_accepts": sorted(k for k, v in broken.items() if v == "WRONG ACCEPT")[:50],
           "broken_wrong_rejects": sorted(k for k, v in broken.items() if v != "WRONG ACCEPT")[:50],
           "score": ((new.get("by_type") or {}).get("ALL") or {}).get("score"),
           "previous_score": ((old.get("by_type") or {}).get("ALL") or {}).get("score"),
           "rules": {r: ids[:20] for r, ids in rules.items()}}
    out["verdict"] = ("worse" if out["broken_wrong_accepts"] else
                      "better" if fixed and not out["broken_wrong_rejects"] else
                      "mixed" if fixed else "same" if not out["broken_wrong_rejects"] else "worse")
    if st is None:
        st = IngestState(key=RELEASE_CHECK_KEY, value={})
        db.add(st)
    st.value = out
    for rule in out["rules"]:
        _event(db, rule, "release_check",
               f"Update {out['commit']} (was {out['previous_commit']}): documents read right before are now accepted "
               f"wrongly: #{', #'.join(out['rules'][rule])}", now=now)
    return out


def refresh(db: Session, now: Optional[datetime] = None) -> dict:
    """Recompute every rule's numbers and state. Commits."""
    from .config import RULE_BREAKER
    now = now or _now()
    since30, since14 = now - timedelta(days=30), now - timedelta(days=CORRECTION_DAYS)
    rows = {r.rule: r for r in db.query(RuleHealth)}
    docs = defaultdict(int)
    types = {}
    for ds, vr, t in db.query(Document.date_source, Document.validity_rule_used, Document.document_type) \
            .filter(Document.uploaded_at >= since30, Document.readability == "READABLE"):
        for rule in {ds, vr} - {None, ""}:
            docs[rule] += 1
            types.setdefault(rule, canonical_type(t))
    corrections = _corrections(db, since14)
    runs = _latest_eval(db, n=1)
    ev = (runs[0].summary or {}) if runs else {}
    ev_at = runs[0].started_at if runs else None
    by_rule = ev.get("by_rule") or {}
    rc = release_check(db, now)
    rc_rules = set((rc or {}).get("rules") or {}) if rc and rc.get("commit") == ev.get("commit") else set()

    changes = []
    for rule in set(rows) | set(docs) | set(corrections) | set(by_rule) | rc_rules:
        h = rows.get(rule)
        if h is None:
            h = RuleHealth(rule=rule, kind=_kind(rule), doc_type=types.get(rule) or _type_of(rule), state="ok",
                           state_since=now)
            db.add(h)
            rows[rule] = h
        released = h.released_at or datetime.min
        e = by_rule.get(rule) or {}
        use_eval = ev_at is not None and ev_at > released
        h.docs_30d = docs.get(rule, 0)
        h.corrected_14d = sum(1 for at, _ in corrections.get(rule, []) if at and at > released)
        h.eval_checked = sum(e.values()) if use_eval else 0
        h.eval_correct = e.get("correct", 0) if use_eval else 0
        h.eval_wrong_accepts = e.get("WRONG ACCEPT", 0) if use_eval else 0
        h.eval_wrong_rejects = e.get("wrong reject", 0) if use_eval else 0
        accepted = h.eval_correct + h.eval_wrong_accepts
        h.precision = round(h.eval_correct / accepted, 3) if accepted else None
        h.updated_at = now
        if h.manual:
            continue
        reasons_p, reasons_w = [], []
        p = h.precision if h.precision is not None else 0
        if (h.eval_wrong_accepts >= 2 and p < PRECISION_FLOOR) or (h.eval_wrong_accepts and p < PRECISION_BREAK):
            reasons_p.append(f"{h.eval_wrong_accepts} wrong accept(s) in the reading check "
                             f"(right {round(100 * (h.precision or 0))}% of the time it accepted)")
        elif h.eval_wrong_accepts:
            reasons_w.append(f"{h.eval_wrong_accepts} wrong accept(s) in the reading check")
        if h.corrected_14d >= 3 and h.corrected_14d >= 0.15 * max(h.docs_30d, 1):
            reasons_p.append(f"a person corrected it {h.corrected_14d} times in {CORRECTION_DAYS} days")
        elif h.corrected_14d:
            reasons_w.append(f"a person corrected it {h.corrected_14d} time(s) in {CORRECTION_DAYS} days")
        if rule in rc_rules and rc["commit"]:
            reasons_p.append(f"update {rc['commit']} made it accept document(s) wrongly that were right before")
        if h.eval_wrong_rejects >= 2:
            reasons_w.append(f"{h.eval_wrong_rejects} wrong rejects in the reading check")
        if reasons_p and (not can_hold(rule) or RULE_BREAKER != "on"):
            reasons_w, reasons_p = reasons_p + reasons_w, []
        state = "probation" if reasons_p else "watch" if reasons_w else "ok"
        reason = "; ".join(reasons_p or reasons_w) or None
        if state != h.state:
            changes.append((rule, h.state, state))
            _event(db, rule, state, (f"Was {h.state}. " + (reason or "The latest check is clean.")
                                     + (" Its new readings wait for a person." if state == "probation" else "")), now=now)
            h.state, h.state_since = state, now
        h.state_reason = reason
    db.commit()
    counts = defaultdict(int)
    for h in rows.values():
        counts[h.state] += 1
    logger.info("rule_health rules=%s changes=%s", dict(counts), changes)
    return {"rules": len(rows), **counts, "changes": [f"{r}: {a} -> {b}" for r, a, b in changes],
            "release_check": (rc or {}).get("verdict")}


def held_rule(db: Session, rules: list[Optional[str]]) -> Optional[RuleHealth]:
    """The first of these rules that is on probation, if any."""
    names = [r for r in rules if can_hold(r)]
    if not names:
        return None
    return db.query(RuleHealth).filter(RuleHealth.rule.in_(names), RuleHealth.state == "probation").first()


def watched(db: Session, rules: list[Optional[str]]) -> list[RuleHealth]:
    names = [r for r in rules if r]
    return db.query(RuleHealth).filter(RuleHealth.rule.in_(names), RuleHealth.state == "watch").all() if names else []


def set_state(db: Session, rule: str, state: str, by: str, note: str = "") -> RuleHealth:
    """A person puts a rule on probation, or releases it (back to ok). A
    release is kept: evidence from before it is not counted again."""
    if state not in ("probation", "ok", "auto"):
        raise ValueError("state must be probation, ok or auto")
    if state == "probation" and not can_hold(rule):
        raise ValueError("This is a policy rule (default / lifetime / typed by a person); the Review queue already checks it.")
    h = db.get(RuleHealth, rule)
    if h is None:
        h = RuleHealth(rule=rule, kind=_kind(rule), doc_type=_type_of(rule), state="ok")
        db.add(h)
    now = _now()
    if state == "auto":
        h.manual = False
        _event(db, rule, "auto", f"The agent decides again. {note}".strip(), by=by, now=now)
    else:
        if state == "ok":
            h.released_at = now
        h.manual = state == "probation"
        h.state, h.state_since = state, now
        h.state_reason = (f"Set by {by}" + (f": {note}" if note else ""))[:500]
        _event(db, rule, "released" if state == "ok" else "held_by_person", note or "", by=by, now=now)
    db.commit()
    return h


def overview(db: Session) -> dict:
    """For the Rules page (Monitoring → Rules & safety)."""
    rows = db.query(RuleHealth).order_by(RuleHealth.rule).all()
    events = db.query(RuleEvent).order_by(RuleEvent.at.desc()).limit(40).all()
    held_now = {}
    for q, d in (db.query(ManualReviewQueue, Document).join(Document, Document.id == ManualReviewQueue.document_id)
                 .filter(ManualReviewQueue.status == ReviewStatus.PENDING, ManualReviewQueue.reason.like("Safety check:%"))):
        risk = (d.extracted_fields or {}).get("risk") or {}
        for r in risk.get("reasons") or []:
            held_now[r.get("code")] = held_now.get(r.get("code"), 0) + 1
    rc = db.get(IngestState, RELEASE_CHECK_KEY)
    order = {"probation": 0, "watch": 1, "ok": 2}
    return {
        "rules": [{"rule": h.rule, "about": describe(h.rule), "kind": h.kind, "type": h.doc_type, "state": h.state,
                   "since": h.state_since, "reason": h.state_reason, "manual": bool(h.manual), "can_hold": can_hold(h.rule),
                   "docs_30d": h.docs_30d, "corrected_14d": h.corrected_14d, "eval_checked": h.eval_checked,
                   "eval_wrong_accepts": h.eval_wrong_accepts, "eval_wrong_rejects": h.eval_wrong_rejects,
                   "precision": h.precision}
                  for h in sorted(rows, key=lambda h: (order.get(h.state, 3), -(h.docs_30d or 0)))],
        "events": [{"rule": e.rule, "at": e.at, "event": e.event, "detail": e.detail, "by": e.by} for e in events],
        "held_waiting": sum(held_now.values()), "held_by_reason": held_now,
        "release_check": rc.value if rc is not None else None,
    }
