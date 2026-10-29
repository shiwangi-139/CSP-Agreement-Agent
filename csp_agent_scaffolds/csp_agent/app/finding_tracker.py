"""
app/finding_tracker.py
Coach findings followed until they are really solved (feedback loop).

Findings come from the week's warning signs (app/signals.py findings) and
from the harness (here: rules on probation, a release check that made reading
worse, sending paused, readings held by the safety gate). Every night
(app/worker.py, 03:20) each one is tracked by its key:

  open       found
  fixing     a person pressed "Fix applied" (with a note of what was done)
  verified   after "Fix applied" it was no longer found: closed
  gone       it went away without a fix (closed)
  reopened   found again after it was closed: a regression, or the fix
             didn't hold; also when it is still found 7 days after "Fix
             applied" (the fix didn't work)

Each change is kept in the finding's history. The Coach shows the status
next to each finding; Monitoring → Rules & safety lists them all.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import CoachFinding, IngestState, ManualReviewQueue, ReviewStatus, RuleHealth

OPEN_RULES = {"label": "Open Rules & safety", "do": "view", "arg": "monitoring:rules"}
FIX_DAYS = 7
CLOSED = ("verified", "gone")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def harness_findings(db: Session, now: Optional[datetime] = None) -> list[dict]:
    from .comms.send_gate import pause_state
    from .rule_health import RELEASE_CHECK_KEY, describe
    now = now or _now()
    out = []

    def add(key, level, title, detail, fix, action=OPEN_RULES):
        out.append({"key": "H:" + key, "level": level, "title": title, "detail": detail, "fix": fix,
                    "examples": [], "action": action})

    for h in db.query(RuleHealth).filter(RuleHealth.state == "probation"):
        add(f"probation:{h.rule}", "critical", f"Rule on probation: {describe(h.rule)}",
            f"{h.state_reason or 'It reads wrongly.'} Its new readings wait for a person meanwhile.",
            "Fix the rule (the reading check lists the documents it got wrong), then the next night's check releases "
            "it; or press Release on Rules & safety if those answers were wrong.")
    rc = db.get(IngestState, RELEASE_CHECK_KEY)
    v = (rc.value or {}) if rc is not None else {}
    if v.get("verdict") == "worse" and v.get("at") and now - datetime.fromisoformat(v["at"]) < timedelta(days=3):
        add(f"release:{v.get('commit')}", "critical",
            f"Update {v.get('commit')} made reading worse: {len(v.get('broken_wrong_accepts') or [])} document(s) "
            "now accepted wrongly",
            f"Compared with {v.get('previous_commit')}: fixed {v.get('fixed', 0)}, newly accepted wrongly "
            f"{len(v.get('broken_wrong_accepts') or [])}, newly refused wrongly {len(v.get('broken_wrong_rejects') or [])}. "
            "The rules that read them are on probation (their readings wait for a person).",
            f"Fix them in a new update, or undo {v.get('commit')} (git revert) and run update.sh.")
    p = pause_state(db)
    if p.get("on"):
        add("paused", "warn", "All sending is paused",
            f"Paused by {p.get('by')} at {p.get('at')}" + (f": {p['reason']}" if p.get("reason") else "")
            + ". Reminders and replies wait.", "Resume sending on Rules & safety when it is safe.")
    held = (db.query(ManualReviewQueue).filter(ManualReviewQueue.status == ReviewStatus.PENDING,
                                               ManualReviewQueue.reason.like("Safety check:%")).count())
    if held:
        add("held", "warn" if held >= 5 else "info", f"{held} reading(s) held by the safety gate wait for a person",
            "The CSPs were told our team is checking them; they count once a person accepts them.",
            "Decide them in the Review queue (each says why it was held).",
            {"label": "Open the Review queue", "do": "go", "arg": "review"})
    return out


def _log(row: CoachFinding, now: datetime, status: str, note: str = "", count=None) -> None:
    hist = list(row.history or [])
    hist.append({"at": now.isoformat(timespec="minutes"), "status": status, "note": note[:200],
                 **({"count": count} if count is not None else {})})
    row.history = hist[-30:]
    row.status = status


def track(db: Session, now: Optional[datetime] = None) -> dict:
    """Update every tracked finding from today's findings. Commits."""
    from .signals import findings
    now = now or _now()
    current = {f["key"]: f for f in findings(db, now) + harness_findings(db, now) if f.get("key")}
    rows = {r.key: r for r in db.query(CoachFinding)}
    moved = []
    for key, f in current.items():
        r = rows.get(key)
        if r is None:
            r = CoachFinding(key=key, first_seen=now, history=[], status="open")
            db.add(r)
            _log(r, now, "open", f["title"])
            moved.append((key, "new"))
        elif r.status in CLOSED:
            _log(r, now, "reopened", "found again after it was closed")
            r.closed_at = None
            moved.append((key, "reopened"))
        elif r.status == "fixing" and r.fix_at and now - r.fix_at > timedelta(days=FIX_DAYS):
            _log(r, now, "reopened", f"still found {FIX_DAYS} days after the fix: it didn't work")
            moved.append((key, "fix failed"))
        r.title, r.level, r.last_seen = f["title"], f["level"], now
    for key, r in rows.items():
        if key in current or r.status in CLOSED:
            continue
        if r.status == "fixing":
            _log(r, now, "verified", "no longer found after the fix")
        else:
            _log(r, now, "gone", "no longer found")
        r.closed_at = now
        moved.append((key, r.status))
    db.commit()
    counts = {}
    for r in db.query(CoachFinding):
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"tracked": sum(counts.values()), **counts, "changes": [f"{k}: {s}" for k, s in moved][:50]}


def mark_fixed(db: Session, key: str, by: str, note: str = "") -> CoachFinding:
    r = db.get(CoachFinding, key)
    if r is None:
        raise ValueError("Unknown finding (it is tracked from the night after it first appears).")
    if r.status in CLOSED:
        raise ValueError("This finding is already closed.")
    now = _now()
    r.fix_at, r.fix_by, r.fix_note = now, by, (note or "")[:500]
    _log(r, now, "fixing", f"fix applied by {by}" + (f": {note}" if note else ""))
    db.commit()
    return r


def annotate(db: Session, items: list[dict]) -> list[dict]:
    """Add each finding's tracked status (for the Coach)."""
    keys = [f["key"] for f in items if f.get("key")]
    rows = {r.key: r for r in db.query(CoachFinding).filter(CoachFinding.key.in_(keys))} if keys else {}
    for f in items:
        r = rows.get(f.get("key"))
        if r is not None:
            f["track"] = {"status": r.status, "since": r.first_seen, "fix_at": r.fix_at, "fix_by": r.fix_by,
                          "fix_note": r.fix_note}
    return items


def overview(db: Session) -> dict:
    order = {"reopened": 0, "open": 1, "fixing": 2, "verified": 3, "gone": 4}
    rows = sorted(db.query(CoachFinding), key=lambda r: (order.get(r.status, 5), -(r.last_seen or datetime(1970, 1, 1)).timestamp()))
    return {"findings": [{"key": r.key, "title": r.title, "level": r.level, "status": r.status, "first_seen": r.first_seen,
                          "last_seen": r.last_seen, "fix_at": r.fix_at, "fix_by": r.fix_by, "fix_note": r.fix_note,
                          "closed_at": r.closed_at, "history": (r.history or [])[-6:]} for r in rows[:60]]}
