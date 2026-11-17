"""
scripts/full_reread.py
The weekly full re-read: every stored document, read again with today's
rules and compared with what is on record. A seventh of them each night
(document id % 7 = the day of the week), so all are read every week.

Read-only: no document, date, Label or message changes. A document that
would now read differently is recorded as a warning sign
(S11_READS_DIFFERENTLY_NOW, app/signals.py) and shows as an Agent Coach
finding: "critical" when the record was decided by a person (today's rules
disagree with a person), "warn" otherwise. It also checks files made smaller
(app/shrink.py): a smaller copy that reads differently shows here.

    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.full_reread              # tonight's seventh
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.full_reread --slice 3
    nice -n 19 env OCR_WORKERS=1 .venv/bin/python -m scripts.full_reread --all --minutes 600

The worker runs it every night at 00:40 (app/worker.py), for at most
FULL_REREAD_MINUTES, when FULL_REREAD is on.
"""
import argparse
import time
from datetime import date, datetime, timezone

from app import vault
from app.compliance import canonical_type
from app.config import FULL_REREAD_MINUTES
from app.db import SessionLocal
from app.models import AgentSignal, CSP, Document, DocumentStatus
from scripts.check_all_documents import ACCEPTED, TYPES, read_isolated

KIND = "S11_READS_DIFFERENTLY_NOW"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def by_person(d: Document) -> bool:
    return d.status == DocumentStatus.MANUAL_VERIFIED or (d.date_source or "") == "MANUAL_REVIEW"


def differences(d: Document, ex: dict) -> list[str]:
    """What today's reading says differently from the record (plain words)."""
    if ex.get("timeout") or ex.get("error"):
        return []                                            # not a difference: it could not be read now
    out = []
    if ex.get("readability") != "READABLE":
        return [f"now not readable ({(ex.get('rejection_reason') or '')[:60]})"]
    if canonical_type(ex.get("document_type")) != canonical_type(d.document_type):
        out.append(f"type {canonical_type(d.document_type)} -> {canonical_type(ex.get('document_type'))}")
    if str(ex.get("start_date")) != str(d.issue_date):
        out.append(f"issued {d.issue_date} -> {ex.get('start_date')}")
    exp = None if ex.get("expiry_date") == "LIFETIME_NO_EXPIRY" else ex.get("expiry_date")
    if str(exp) != str(d.expiry_date):
        out.append(f"expires {d.expiry_date} -> {exp}")
    return out


def record(db, d: Document, csp, diffs: list[str], ex: dict, now: datetime) -> bool:
    """One warning sign per document and reading (running again adds nothing)."""
    key = f"{d.id}:{ex.get('document_type')}:{ex.get('start_date')}:{ex.get('expiry_date')}"
    row = db.query(AgentSignal).filter(AgentSignal.kind == KIND, AgentSignal.key == key).first()
    if row is not None:
        row.last_seen = row.happened_at = now
        return False
    person = by_person(d)
    db.add(AgentSignal(kind=KIND, key=key, strength="strong" if person else "medium", csp_id=d.csp_id,
                       document_id=d.id, rule=ex.get("date_source") or ex.get("validity_rule_used"),
                       detail=("Decided by a person; " if person else "") + "today's rules read: " + "; ".join(diffs),
                       facts={"type": canonical_type(d.document_type), "by_person": person,
                              "rule": ex.get("validity_rule_used"), "date_rule": ex.get("date_source"),
                              "shrunk": bool((d.extracted_fields or {}).get("shrunk")),
                              "dc": None, "state": csp.state if csp else None},
                       happened_at=now, first_seen=now, last_seen=now, status="NEW"))
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slice", type=int, choices=range(7), help="which seventh (default: today's weekday)")
    ap.add_argument("--all", action="store_true", help="every document, not a seventh")
    ap.add_argument("--minutes", type=int, default=FULL_REREAD_MINUTES)
    ap.add_argument("--timeout", type=int, default=240, help="seconds allowed per document")
    a = ap.parse_args()
    part = date.today().weekday() if a.slice is None else a.slice
    started, began = _now(), time.time()
    db = SessionLocal()
    counts = {"read": 0, "same": 0, "different": 0, "new_signs": 0, "could_not_read": 0, "missing_file": 0}
    try:
        docs = (db.query(Document).filter(Document.storage_path.isnot(None), Document.document_type.in_(TYPES),
                                          Document.status.in_(ACCEPTED)).order_by(Document.id).all())
        if not a.all:
            docs = [d for d in docs if d.id % 7 == part]
        print(f"{len(docs)} document(s) to read {'(all)' if a.all else f'(seventh {part})'}; "
              f"stopping after {a.minutes} minutes.", flush=True)
        now = _now()
        for d in docs:
            if time.time() - began > a.minutes * 60:
                print("Time is up: the rest is read next week.", flush=True)
                break
            path = vault.abs_path(d.storage_path)
            if path is None or not path.exists():
                counts["missing_file"] += 1
                continue
            ex = read_isolated(path, d.document_type, a.timeout)
            counts["read"] += 1
            if ex.get("timeout") or ex.get("error"):
                counts["could_not_read"] += 1
                continue
            diffs = differences(d, ex)
            if not diffs:
                counts["same"] += 1
                continue
            counts["different"] += 1
            csp = db.get(CSP, d.csp_id)
            counts["new_signs"] += record(db, d, csp, diffs, ex, now)
            db.commit()
            print(f"{csp.current_code if csp else '?'} doc {d.id} {canonical_type(d.document_type)}: "
                  + "; ".join(diffs), flush=True)
        from app.monitoring import record_run
        record_run(db, "full_reread", started, "OK", {"slice": None if a.all else part, **counts,
                                                      "minutes": round((time.time() - began) / 60, 1)})
        print(f"\nRead {counts['read']}: {counts['same']} the same, {counts['different']} differently "
              f"({counts['new_signs']} new), {counts['could_not_read']} could not be read, "
              f"{counts['missing_file']} file(s) missing. Nothing was changed.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
