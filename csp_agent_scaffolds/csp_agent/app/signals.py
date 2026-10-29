"""
app/signals.py
The agent's own warning signs: places in its records where it probably read
a document wrongly, or could not decide on its own. Found every night
(app/worker.py, 03:00) from data it already keeps, so nobody has to review
anything for it. Step 1 of the new feedback loop: signals are only RECORDED
(table agent_signals); no document, CSP, message or rule changes. The Agent
Coach groups them into findings in step 2.

  S1  refused, then accepted     refused at intake, an accepted copy of the same
                                 type came for the CSP within 30 days
  S2  same file sent again       a file identical to a refused one came again
                                 (e-mail or upload link)
  S3  two readings disagree      two copies, same issue date but another expiry
                                 (or the reverse, issue within 3 days)
  S4  impossible result          expiry before issue, issue after arrival,
                                 over 6 years, an accepted agreement under 10
                                 pages, an accepted IIBF without a reg. number
  S5  guessed validity, words    the 1-year default used although the text has
                                 validity wording (or "validity hidden")
  S6  a person corrected it      a Review item closed as corrected, or rejected
                                 after the agent had accepted the document
  S7  CSP asked after a refusal  an agreement / upload question within 7 days
                                 after one of their documents was refused
  S8  repeated upload failures   3+ refused uploads in 7 days, same reason
  S9  rule drift                 this week's share of a validity rule, or the
                                 unreadable rate of a channel, doubled or halved
                                 against the 4 weeks before (10+ documents)
  S10 could not decide           every Review item and unreadable file, with
                                 its cause and where it came from

Times: Document.uploaded_at and ManualReviewQueue times are written by the app
in UTC; CspQuestion / AgreementEvent / InboundMessage times by the database
(India). The windows are days wide, so the 5.5 h difference doesn't matter.
"""
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .accuracy import how_read
from .compliance import canonical_type
from .models import (AgentSignal, AgreementEvent, CSP, CspQuestion, Document, DocumentStatus, ExtractionCorrection,
                     InboundMessage, InternalUser, ManualReviewQueue, ReviewStatus)
from .safety_gate import impossible

TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")
LOOKBACK_DAYS = 30
KEEP_DAYS = 180
ACCEPTED = (DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED)
REFUSED = (DocumentStatus.REJECTED, DocumentStatus.UNREADABLE)

KINDS = {
    "S1_REFUSED_THEN_ACCEPTED": "Refused, then accepted",
    "S2_SAME_FILE_AGAIN": "Same file sent again after a refusal",
    "S3_READINGS_DISAGREE": "Two readings disagree",
    "S4_IMPOSSIBLE_RESULT": "Impossible result",
    "S5_GUESSED_VALIDITY_WORDS": "Guessed validity, validity words present",
    "S6_PERSON_CORRECTED": "A person corrected the agent",
    "S7_QUESTION_AFTER_REFUSAL": "CSP asked after a refusal",
    "S8_REPEATED_UPLOAD_FAILURES": "Repeated upload failures",
    "S9_RULE_DRIFT": "Rule drift",
    "S10_REVIEW_CAUSE": "Could not decide on its own",
}

# S10: why a document went to a person or could not be read. Deliberate
# checks are policy, not something the agent could fix.
DELIBERATE = ("deliberate check", "DC link upload (policy)")
VALIDITY_WORDS = re.compile(r"valid\w*\s+(?:for|upto|up\s+to|till|until|from)|validity|period\s+of|ending\s+on|"
                            r"remain\s+valid|मान्य|वैध", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def review_cause(reason: Optional[str], doc: Optional[Document] = None) -> str:
    """The cause of a Review item (its reason) or of an unreadable file."""
    r = (reason or "").lower()
    if r.startswith("safety check"):
        return "held: risky reading"
    if r.startswith("risk check"):
        return "risk check (counted meanwhile)"
    method = ((doc.extraction_method if doc is not None else None) or "").upper()
    if r.startswith(("spot check", "reference check")):
        return "deliberate check"
    if r.startswith("dc upload"):
        return "DC link upload (policy)"
    if r.startswith("validity check") or "no validity period" in r or "number is hidden" in r:
        return "validity guessed"
    if "check which is right" in r or "differs" in r or "typed issue date" in r:
        return "dates conflict"
    if "dates read by the ai model" in r:
        return "dates only from the AI model"
    if "names" in r or "not confirmed" in r or "could not confirm" in r:
        return "name or CSP not confirmed"
    if ("only from" in r or "wording" in r or "box but reads as" in r or "not the agreement" in r
            or method in ("UNREADABLE_UNKNOWN_TYPE", "NOT_THE_AGREEMENT", "ALLOWLIST_GATE_UNKNOWN",
                          "ALLOWLIST_GATE_NOT_THE_DOCUMENT")):
        return "type unclear"
    if "issue date could not be read" in r or method == "UNREADABLE_NO_ISSUE_DATE":
        return "issue date not found"
    if (doc is not None and doc.readability == "UNREADABLE") or "could not read" in r or "ocr could not" in r \
            or "could not be opened" in r:
        return "file not readable"
    return "other"


class _Collector:
    def __init__(self, db: Session, now: datetime):
        self.db, self.now, self.added, self.seen = db, now, Counter(), Counter()
        self.existing = {(k, key): i for i, k, key in db.query(AgentSignal.id, AgentSignal.kind, AgentSignal.key)}

    def add(self, kind: str, key: str, strength: str, detail: str, *, csp_id=None, document_id=None, rule=None,
            facts=None, happened_at=None):
        self.seen[kind] += 1
        row_id = self.existing.get((kind, key))
        if row_id is not None:
            if row_id > 0:
                self.db.query(AgentSignal).filter(AgentSignal.id == row_id).update({"last_seen": self.now})
            return
        self.db.add(AgentSignal(kind=kind, key=key, strength=strength, csp_id=csp_id, document_id=document_id,
                                rule=rule, detail=detail[:300], facts=facts or {}, happened_at=happened_at,
                                first_seen=self.now, last_seen=self.now))
        self.existing[(kind, key)] = -1
        self.added[kind] += 1


def _fmt(d) -> str:
    return d.strftime("%d-%m-%Y") if d else "?"


def _facts(doc: Document, csp: Optional[CSP], dcs: dict) -> dict:
    ex = doc.extracted_fields or {}
    return {"type": canonical_type(doc.document_type), "how_read": how_read(doc), "channel": doc.upload_channel,
            "pages_readable": ex.get("pages_readable"), "page_count": ex.get("page_count"),
            "state": csp.state if csp is not None else None,
            "dc": dcs.get(csp.dc_id) if csp is not None else None}


def collect(db: Session, now: Optional[datetime] = None, days: int = LOOKBACK_DAYS) -> dict:
    """Find this window's signals and record the new ones. Returns how many of
    each kind were seen and how many were new. Changes nothing else."""
    now = now or _now()
    since = now - timedelta(days=days)
    col = _Collector(db, now)
    dcs = {u.id: u.name for u in db.query(InternalUser)}
    csps = {c.id: c for c in db.query(CSP)}
    docs = [d for d in db.query(Document).filter(Document.uploaded_at >= since - timedelta(days=30),
                                                 Document.uploaded_at <= now)
            if canonical_type(d.document_type) in TYPES]
    recent = [d for d in docs if d.uploaded_at >= since]
    by_csp_type = defaultdict(list)
    for d in docs:
        if d.csp_id:
            by_csp_type[(d.csp_id, canonical_type(d.document_type))].append(d)
    person_rejected = {i for (i,) in db.query(ManualReviewQueue.document_id)
                       .filter(ManualReviewQueue.status == ReviewStatus.REJECTED)}

    # S1: refused at intake, an accepted copy of the same type came later.
    for d in recent:
        if d.status not in REFUSED or not d.csp_id or d.id in person_rejected:
            continue
        later = [x for x in by_csp_type[(d.csp_id, canonical_type(d.document_type))]
                 if x.status in ACCEPTED and d.uploaded_at < x.uploaded_at <= d.uploaded_at + timedelta(days=30)]
        if later:
            x = min(later, key=lambda y: y.uploaded_at)
            same = bool(d.issue_date and d.issue_date == x.issue_date)
            why = (d.extracted_fields or {}).get("readability_reason") or d.status.value.lower()
            col.add("S1_REFUSED_THEN_ACCEPTED", str(d.id), "strong" if same else "medium",
                    f"Refused on {_fmt(d.uploaded_at)} ({why}); an accepted copy (#{x.id}) came on "
                    f"{_fmt(x.uploaded_at)}" + (", same issue date" if same else ""),
                    csp_id=d.csp_id, document_id=d.id, rule=d.extraction_method,
                    facts={**_facts(d, csps.get(d.csp_id), dcs), "accepted_copy": x.id}, happened_at=x.uploaded_at)

    # S2: a file identical to a refused one came again (e-mail or upload link).
    refused = {d.id: d for d in db.query(Document).filter(Document.status.in_(REFUSED))}
    for m in db.query(InboundMessage).filter(InboundMessage.received_at >= since, InboundMessage.received_at <= now):
        for a in m.attachment_decisions or []:
            d = refused.get(a.get("document_id"))
            if a.get("decision") == "DUPLICATE" and d is not None:
                col.add("S2_SAME_FILE_AGAIN", f"mail:{m.id}:{d.id}", "strong",
                        f"The same file as refused document #{d.id} was e-mailed again ({a.get('filename') or 'file'})",
                        csp_id=d.csp_id, document_id=d.id, rule=d.extraction_method,
                        facts=_facts(d, csps.get(d.csp_id), dcs), happened_at=m.received_at)
    events = (db.query(AgreementEvent).filter(AgreementEvent.event_type == "PORTAL_UPLOAD", AgreementEvent.sent_at >= since,
                                              AgreementEvent.sent_at <= now)
              .order_by(AgreementEvent.sent_at).all())
    for e in events:
        for r in (e.payload or {}).get("results") or []:
            d = refused.get(r.get("document_id"))
            if r.get("reason") in ("dup_rejected", "dup_unreadable") and d is not None:
                col.add("S2_SAME_FILE_AGAIN", f"upload:{e.id}:{d.id}", "strong",
                        f"The same file as refused document #{d.id} was uploaded again ({e.source or 'upload link'})",
                        csp_id=d.csp_id, document_id=d.id, rule=d.extraction_method,
                        facts=_facts(d, csps.get(d.csp_id), dcs), happened_at=e.sent_at)

    # S3: two copies of one CSP's document read with different dates.
    read = (DocumentStatus.VALID, DocumentStatus.EXPIRED, DocumentStatus.MANUAL_VERIFIED, DocumentStatus.NEEDS_REVIEW)
    for (cid, t), group in by_csp_type.items():
        g = sorted((x for x in group if x.status in read and x.issue_date and x.expiry_date), key=lambda y: y.id)
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                if b.uploaded_at < since:
                    continue
                gap = abs((a.issue_date - b.issue_date).days)
                if (gap == 0 and a.expiry_date != b.expiry_date) or (0 < gap <= 3 and a.expiry_date == b.expiry_date):
                    col.add("S3_READINGS_DISAGREE", f"{a.id}-{b.id}", "medium",
                            f"#{a.id} read {_fmt(a.issue_date)} → {_fmt(a.expiry_date)} ({a.validity_rule_used}); "
                            f"#{b.id} read {_fmt(b.issue_date)} → {_fmt(b.expiry_date)} ({b.validity_rule_used})",
                            csp_id=cid, document_id=b.id, rule=b.validity_rule_used,
                            facts={**_facts(b, csps.get(cid), dcs), "other": a.id}, happened_at=b.uploaded_at)

    # S4: results that cannot be right.
    for d in recent:
        if d.readability != "READABLE" or d.status in REFUSED:
            continue
        t, ex = canonical_type(d.document_type), d.extracted_fields or {}
        arrived = (d.source_date or d.uploaded_at).date()
        problems = impossible(d.issue_date, d.expiry_date, arrived)
        if t == "AGREEMENT" and d.status in ACCEPTED and ex.get("page_count") and ex["page_count"] < 10:
            problems.append(("short_agreement", f"accepted agreement with only {ex['page_count']} page(s)"))
        if t == "IIBF_CERTIFICATE" and d.status in ACCEPTED and not d.iibf_reg_number:
            problems.append(("iibf_no_number", "accepted IIBF certificate without a registration number"))
        for code, text in problems:
            col.add("S4_IMPOSSIBLE_RESULT", f"{d.id}:{code}", "strong", text[0].upper() + text[1:], csp_id=d.csp_id,
                    document_id=d.id, rule=d.validity_rule_used or d.date_source,
                    facts={**_facts(d, csps.get(d.csp_id), dcs), "problem": code}, happened_at=d.uploaded_at)

    # S5: the 1-year default although the text speaks of validity.
    for d in recent:
        rule = d.validity_rule_used or ""
        if not rule.endswith(("DEFAULT_1_YEAR", "DEFAULT_2_YEAR")) or d.status in REFUSED:
            continue
        ex = d.extracted_fields or {}
        texts = " ".join(x.get("text") or "" for x in ((ex.get("why") or {}).get("lines") or []))
        m = VALIDITY_WORDS.search(texts)
        if ex.get("validity_hidden") or m:
            col.add("S5_GUESSED_VALIDITY_WORDS", str(d.id), "medium",
                    "Validity hidden under a stamp" if ex.get("validity_hidden") else
                    f"1-year default used, but the text has \"{texts[max(0, m.start() - 30): m.end() + 30].strip()}\"",
                    csp_id=d.csp_id, document_id=d.id, rule=rule, facts=_facts(d, csps.get(d.csp_id), dcs),
                    happened_at=d.uploaded_at)

    # S6: a person corrected the agent.
    docs_by_id = {d.id: d for d in docs}
    for q in db.query(ManualReviewQueue).filter(ManualReviewQueue.resolved_at >= since, ManualReviewQueue.resolved_at <= now,
                                                ManualReviewQueue.status.in_([ReviewStatus.CORRECTED, ReviewStatus.REJECTED])):
        agent_had_accepted = (q.reason or "").lower().startswith(("spot check", "reference check", "validity check", "risk check"))
        if q.status == ReviewStatus.REJECTED and not agent_had_accepted:
            continue
        d = docs_by_id.get(q.document_id) or db.get(Document, q.document_id)
        what = "corrected the dates" if q.status == ReviewStatus.CORRECTED else "rejected a document the agent had accepted"
        col.add("S6_PERSON_CORRECTED", f"q:{q.id}", "strong", f"A person {what} ({(q.reason or '')[:80]})",
                csp_id=d.csp_id if d else None, document_id=q.document_id, rule=d.validity_rule_used if d else None,
                facts=_facts(d, csps.get(d.csp_id), dcs) if d else {}, happened_at=q.resolved_at)
    for c in db.query(ExtractionCorrection).filter(ExtractionCorrection.corrected_at >= since,
                                                   ExtractionCorrection.corrected_at <= now):
        d = docs_by_id.get(c.document_id) or db.get(Document, c.document_id)
        col.add("S6_PERSON_CORRECTED", f"c:{c.id}", "strong",
                f"A person changed {c.field_name}: {c.ai_value} → {c.human_value}",
                csp_id=d.csp_id if d else None, document_id=c.document_id, rule=c.field_name,
                facts=_facts(d, csps.get(d.csp_id), dcs) if d else {}, happened_at=c.corrected_at)

    # S7 / S8: what CSPs ran into on the upload link.
    failures = defaultdict(list)                        # (csp, reason) -> [time]
    for e in events:
        for r in (e.payload or {}).get("results") or []:
            if r.get("ok") is False and e.csp_id and e.sent_at:
                failures[(e.csp_id, r.get("reason") or "?")].append(e.sent_at)
    refused_at = defaultdict(list)
    for d in docs:
        if d.status in REFUSED and d.csp_id:
            refused_at[d.csp_id].append(d.uploaded_at)
    for (cid, _), times in failures.items():
        refused_at[cid] += times
    for q in db.query(CspQuestion).filter(CspQuestion.created_at >= since, CspQuestion.created_at <= now,
                                          CspQuestion.category.in_(["AGREEMENT", "UPLOAD_PROBLEM"])):
        before = [t for t in refused_at.get(q.csp_id, []) if q.created_at - timedelta(days=7) <= t <= q.created_at]
        if before:
            col.add("S7_QUESTION_AFTER_REFUSAL", str(q.id), "weak",
                    f"Asked ({q.category.lower().replace('_', ' ')}) {(q.text or '')[:100]!r} after a refusal on "
                    f"{_fmt(max(before))}", csp_id=q.csp_id, rule=q.category, facts={"category": q.category},
                    happened_at=q.created_at)
    for (cid, reason), times in failures.items():
        times = sorted(times)
        for i in range(len(times) - 2):
            if times[i + 2] - times[i] <= timedelta(days=7):
                c = csps.get(cid)
                n = sum(1 for t in times if times[i] <= t <= times[i] + timedelta(days=7))
                col.add("S8_REPEATED_UPLOAD_FAILURES", f"{cid}:{reason}:{times[i]:%Y-%m-%d}", "medium",
                        f"{n} uploads refused in 7 days for the same reason ({reason})", csp_id=cid, rule=reason,
                        facts={"reason": reason, "state": c.state if c else None, "dc": dcs.get(c.dc_id) if c else None},
                        happened_at=times[i + 2])
                break

    # S9: a rule or a channel suddenly behaves differently.
    week = [d for d in docs if d.uploaded_at >= now - timedelta(days=7)]
    base = [d for d in docs if now - timedelta(days=35) <= d.uploaded_at < now - timedelta(days=7)]
    wk = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    rules_now = Counter(d.validity_rule_used for d in week if d.validity_rule_used)
    rules_base = Counter(d.validity_rule_used for d in base if d.validity_rule_used)
    for name in set(rules_now) | set(rules_base):
        n, b = rules_now.get(name, 0), rules_base.get(name, 0)
        if max(n, b / 4) < 10:                          # too few in a week to judge
            continue
        s_now, s_base = n / (len(week) or 1), b / (len(base) or 1)
        if s_base == 0 or s_now / s_base >= 2 or s_now / s_base <= 0.5:
            col.add("S9_RULE_DRIFT", f"validity rule:{name}:{wk}", "medium",
                    f"Validity rule {name}: {s_now:.0%} of this week's documents against {s_base:.0%} the 4 weeks before",
                    rule=name, facts={"what": "validity rule", "now": round(s_now, 3), "before": round(s_base, 3),
                                      "this_week": n}, happened_at=now)
    per_now, per_base = Counter(d.upload_channel for d in week), Counter(d.upload_channel for d in base)
    bad_now = Counter(d.upload_channel for d in week if d.readability == "UNREADABLE")
    bad_base = Counter(d.upload_channel for d in base if d.readability == "UNREADABLE")
    for ch in per_now:
        if per_now[ch] < 10 or not per_base.get(ch):
            continue
        s_now, s_base = bad_now.get(ch, 0) / per_now[ch], bad_base.get(ch, 0) / per_base[ch]
        if (s_now or s_base) and (s_base == 0 or s_now / s_base >= 2 or s_now / s_base <= 0.5):
            col.add("S9_RULE_DRIFT", f"unreadable rate:{ch}:{wk}", "medium",
                    f"Unreadable rate for {ch}: {s_now:.0%} this week against {s_base:.0%} the 4 weeks before",
                    rule=ch, facts={"what": "unreadable rate", "now": round(s_now, 3), "before": round(s_base, 3),
                                    "this_week": per_now[ch]}, happened_at=now)

    # S10: everything that went to a person, or could not be read, with its cause.
    for q in db.query(ManualReviewQueue).filter(ManualReviewQueue.created_at >= since, ManualReviewQueue.created_at <= now):
        d = docs_by_id.get(q.document_id) or db.get(Document, q.document_id)
        cause = review_cause(q.reason, d)
        col.add("S10_REVIEW_CAUSE", f"q:{q.id}", "info", f"{cause}: {(q.reason or '')[:160]}",
                csp_id=d.csp_id if d else None, document_id=q.document_id, rule=cause,
                facts={**(_facts(d, csps.get(d.csp_id), dcs) if d else {}), "cause": cause,
                       "fixable": cause not in DELIBERATE, "queue_status": q.status.value}, happened_at=q.created_at)
    for d in recent:
        if d.readability == "UNREADABLE" or d.status == DocumentStatus.UNREADABLE:
            ex = d.extracted_fields or {}
            reason = ex.get("readability_reason") or ex.get("rejection_reason")
            cause = review_cause(reason, d)
            col.add("S10_REVIEW_CAUSE", f"d:{d.id}", "info", f"{cause}: {(reason or 'unreadable')[:160]}",
                    csp_id=d.csp_id, document_id=d.id, rule=cause,
                    facts={**_facts(d, csps.get(d.csp_id), dcs), "cause": cause, "fixable": True,
                           "queue_status": "UNREADABLE"}, happened_at=d.uploaded_at)

    removed = db.query(AgentSignal).filter(AgentSignal.last_seen < now - timedelta(days=KEEP_DAYS)).delete()
    db.commit()
    return {"seen": dict(col.seen), "new": dict(col.added), "removed": removed}


# ------------------------------------------------------------ for the dashboard
def overview(db: Session, now: Optional[datetime] = None) -> dict:
    """Monitoring → "Agent's own warnings": counts per kind this week and last
    week, and the Review-queue causes of the last 30 days with where they come
    from and a few examples."""
    from .models import MonitorRun
    now = now or _now()
    wk, prev = now - timedelta(days=7), now - timedelta(days=14)
    rows = (db.query(AgentSignal).filter(AgentSignal.happened_at >= now - timedelta(days=30),
                                         AgentSignal.happened_at <= now).all())
    csps = {c.id: c for c in db.query(CSP)}
    kinds = []
    for k, label in KINDS.items():
        mine = [r for r in rows if r.kind == k]
        kinds.append({"kind": k, "label": label,
                      "this_week": sum(1 for r in mine if r.happened_at >= wk),
                      "last_week": sum(1 for r in mine if prev <= r.happened_at < wk),
                      "days_30": len(mine),
                      "strength": Counter(r.strength for r in mine).most_common(1)[0][0] if mine else None})
    causes = [r for r in rows if r.kind == "S10_REVIEW_CAUSE"]
    by_cause = Counter((r.facts or {}).get("cause", "other") for r in causes)
    fixable = sum(1 for r in causes if (r.facts or {}).get("fixable"))
    detail = []
    for cause, n in by_cause.most_common():
        mine = [r for r in causes if (r.facts or {}).get("cause", "other") == cause]

        def top(field):
            return [[k or "unknown", v] for k, v in Counter((r.facts or {}).get(field) for r in mine).most_common(3)]
        examples = []
        for r in sorted(mine, key=lambda x: x.happened_at, reverse=True)[:3]:
            c = csps.get(r.csp_id)
            examples.append({"document_id": r.document_id, "csp_id": r.csp_id, "csp_code": c.current_code if c else None,
                             "csp_name": c.name if c else None, "detail": r.detail})
        detail.append({"cause": cause, "count": n, "fixable": cause not in DELIBERATE, "how_read": top("how_read"),
                       "channel": top("channel"), "type": top("type"), "state": top("state"), "examples": examples})
    last = db.query(MonitorRun).filter(MonitorRun.kind == "job:signals").order_by(MonitorRun.started_at.desc()).first()
    total_week = sum(k["this_week"] for k in kinds if k["kind"] != "S10_REVIEW_CAUSE")
    strong = sum(1 for r in rows if r.happened_at >= wk and r.strength == "strong")
    status = ({"level": "warn", "text": f"{strong} strong warning sign(s) this week"} if strong else
              {"level": "ok", "text": f"{total_week} warning sign(s) this week, none strong"})
    return {"status": status, "kinds": kinds, "last_run": last.started_at.isoformat() + "Z" if last else None,
            "causes": {"total": len(causes), "fixable": fixable,
                       "fixable_share": round(fixable / len(causes), 3) if causes else None, "by_cause": detail}}


def rows_for_excel(db: Session, days: int = 30, now: Optional[datetime] = None) -> list[list]:
    """One row per signal of the last `days` days, for the Excel download."""
    now = now or _now()
    csps = {c.id: c for c in db.query(CSP)}
    out = []
    for r in (db.query(AgentSignal).filter(AgentSignal.happened_at >= now - timedelta(days=days))
              .order_by(AgentSignal.kind, AgentSignal.happened_at.desc())):
        c = csps.get(r.csp_id)
        f = r.facts or {}
        out.append([KINDS.get(r.kind, r.kind), r.strength, r.happened_at, c.current_code if c else "", c.name if c else "",
                    r.document_id, f.get("type") or "", f.get("cause") or "", f.get("how_read") or "",
                    f.get("channel") or "", f.get("state") or "", f.get("dc") or "", r.rule or "", r.detail or ""])
    return out


# ------------------------------------------------------------ step 2: findings
# The week's signs grouped into findings for the Agent Coach: what is wrong,
# the numbers, a few examples and a suggested fix. Plain counting on the
# agent's own records (no AI model); a person decides the fix.
WEEK_DAYS = 7
OPEN_SIGNS = {"label": "See the warning signs", "do": "view", "arg": "monitoring:signals"}
CAUSE_FIX = {
    "issue date not found": "Most of these are {how}: improve reading for that kind of file, or ask CSPs for a clear "
                            "scanned PDF (the upload page tips).",
    "dates only from the AI model": "The rules found no date in these; look at the examples and add the wording they use "
                                    "to the reading rules, so the AI model is not needed.",
    "validity guessed": "Find the validity wording these agreements use (e.g. 'valid for …', 'ending on …') and add it "
                        "to the agreement rules.",
    "type unclear": "Check what was uploaded: if it is a valid document, teach the type rules its wording; if it is "
                    "the wrong paper, the upload page message can say so more clearly.",
    "name or CSP not confirmed": "The name on the document is spelled differently from the calling sheet: check the "
                                 "examples and correct the sheet or the name match.",
    "dates conflict": "The typed or printed dates disagree with what was read: check the examples to see which rule "
                      "reads the wrong date.",
    "file not readable": "Mostly {how}: ask CSPs for clearer scans, or improve reading of this kind of file.",
    "held: risky reading": "The safety gate held these (impossible dates, a rule on probation, or a disagreeing "
                           "copy): see the reasons on the examples and fix the rule that read them.",
    "risk check (counted meanwhile)": "Medium-risk readings (another date printed, a weak scan, a rule on watch): if "
                                      "people mostly accept them unchanged, raise RISK_CHECK_AT; if they correct "
                                      "them, fix the rule.",
    "other": "Look at the examples: these reasons are not sorted yet.",
}
IMPOSSIBLE_FIX = {
    "expiry_before_issue": "a rule took the wrong date as the issue or the expiry date",
    "issue_after_arrival": "the issue date was read from a later date on the page (an end date or a stamp)",
    "over_6_years": "a validity rule gave a period that is too long",
    "short_agreement": "accepted agreements have pages missing: ask these CSPs for the full agreement",
    "iibf_no_number": "the IIBF registration number was not read",
}


def _top(rows, field: str) -> Optional[str]:
    c = Counter((r.facts or {}).get(field) for r in rows if (r.facts or {}).get(field))
    return c.most_common(1)[0][0] if c else None


def _examples(rows, csps: dict, n: int = 3) -> list[dict]:
    out, seen = [], set()
    for r in sorted(rows, key=lambda x: x.happened_at, reverse=True):
        c = csps.get(r.csp_id)
        if c is None or c.id in seen:
            continue
        seen.add(c.id)
        out.append({"csp_id": c.id, "code": c.current_code, "name": c.name, "detail": r.detail})
        if len(out) == n:
            break
    return out


def findings(db: Session, now: Optional[datetime] = None) -> list[dict]:
    """This week's warning signs as Coach findings, most urgent first."""
    now = now or _now()
    rows = (db.query(AgentSignal).filter(AgentSignal.happened_at >= now - timedelta(days=WEEK_DAYS),
                                         AgentSignal.happened_at <= now).all())
    csps = {c.id: c for c in db.query(CSP).filter(CSP.id.in_({r.csp_id for r in rows if r.csp_id}))}
    by_kind = defaultdict(list)
    for r in rows:
        by_kind[r.kind].append(r)
    out: list[dict] = []

    def add(level, title, detail, fix, mine):
        # key: the title without its numbers, the same from week to week (app/finding_tracker.py)
        out.append({"key": "S:" + re.sub(r"\d+", "#", title)[:200], "level": level, "title": title, "detail": detail, "fix": fix,
                    "examples": _examples(mine, csps), "action": OPEN_SIGNS})

    def human(x):
        return (x or "unknown").replace("_", " ").lower()

    impossible = defaultdict(list)
    for r in by_kind["S4_IMPOSSIBLE_RESULT"]:
        impossible[(r.facts or {}).get("problem", "?")].append(r)
    for problem, mine in sorted(impossible.items(), key=lambda kv: -len(kv[1])):
        add("critical", f"{len(mine)} impossible reading(s) this week: {human(problem)}",
            f"Mostly {human(_top(mine, 'type'))} read from {human(_top(mine, 'how_read'))}.",
            f"Likely cause: {IMPOSSIBLE_FIX.get(problem, 'a rule read the wrong date')}.", mine)

    corrected = defaultdict(list)
    for r in by_kind["S6_PERSON_CORRECTED"]:
        corrected[r.rule or "unknown rule"].append(r)
    for rule, mine in sorted(corrected.items(), key=lambda kv: -len(kv[1])):
        if len(mine) >= 2:
            add("warn", f"People corrected the agent {len(mine)} times on {human(rule)}",
                f"Mostly {human(_top(mine, 'type'))}, read from {human(_top(mine, 'how_read'))}.",
                f"The rule '{human(rule)}' gave answers a person had to change: compare the examples and adjust it.", mine)

    refused = defaultdict(list)
    for r in by_kind["S1_REFUSED_THEN_ACCEPTED"] + by_kind["S2_SAME_FILE_AGAIN"]:
        refused[r.rule or "unknown"].append(r)
    for rule, mine in sorted(refused.items(), key=lambda kv: -len(kv[1])):
        again = sum(1 for r in mine if r.kind == "S2_SAME_FILE_AGAIN")
        if len(mine) >= 3 or again:
            add("warn", f"{len({r.csp_id for r in mine})} CSP(s) were refused, then sent it again or got it accepted",
                f"Refused by {human(rule)}; {again} sent the very same file again." if again else
                f"Refused by {human(rule)}; an accepted copy came later.",
                f"The refusal '{human(rule)}' may be too strict: open the refused files and check whether they were readable.",
                mine)

    for kind, threshold, title, fix in (
            ("S3_READINGS_DISAGREE", 2, "{n} documents were read with different dates than another copy",
             "The same document gave two readings: the rule is unstable on these scans; compare the two copies."),
            ("S5_GUESSED_VALIDITY_WORDS", 3, "{n} agreements got the 1-year default although they mention validity",
             "Their validity wording is not recognised (or a stamp hides it): add the wording to the agreement rules."),
            ("S7_QUESTION_AFTER_REFUSAL", 3, "{n} CSPs asked a question right after a refusal",
             "The refusal message may be unclear: check what they asked and improve the message for that reason.")):
        mine = by_kind[kind]
        if len(mine) >= threshold:
            add("warn", title.format(n=len(mine)), f"Mostly {human(_top(mine, 'type'))}.", fix, mine)

    stuck = defaultdict(list)
    for r in by_kind["S8_REPEATED_UPLOAD_FAILURES"]:
        stuck[r.rule or "?"].append(r)
    for reason, mine in sorted(stuck.items(), key=lambda kv: -len(kv[1])):
        if len(mine) >= 2:
            add("warn", f"{len(mine)} CSPs keep failing on the upload page: {human(reason)}",
                "Each tried 3 or more times in a week with the same refusal.",
                "Call them, or make the upload page message for this reason clearer.", mine)

    for r in by_kind["S9_RULE_DRIFT"]:
        add("warn", f"Rule drift: {r.detail}", "A rule or channel behaves differently from the 4 weeks before.",
            "Check what changed this week (a new code change, a new document format, a channel problem).", [r])

    causes = by_kind["S10_REVIEW_CAUSE"]
    if causes:
        fixable = [r for r in causes if (r.facts or {}).get("fixable")]
        by_cause = Counter((r.facts or {}).get("cause", "other") for r in fixable)
        if by_cause:
            cause, n = by_cause.most_common(1)[0]
            mine = [r for r in fixable if (r.facts or {}).get("cause", "other") == cause]
            how = human(_top(mine, "how_read"))
            if n >= 5 and n >= 0.25 * len(causes):
                state = _top(mine, "state")
                add("warn", f"{round(100 * n / len(causes))}% of this week's documents for a person: {cause}",
                    f"{n} of {len(causes)}; mostly {human(_top(mine, 'type'))} read from {how}"
                    + (f", from {state}" if state else "") + ".",
                    CAUSE_FIX.get(cause, CAUSE_FIX["other"]).format(how=how), mine)
        add("info", f"{len(causes)} documents went to a person this week; the agent could have decided "
                    f"{round(100 * len(fixable) / len(causes))}% itself",
            "The rest are policy checks (spot checks, DC link uploads).",
            "Fix the biggest cause first; the next night's reading check shows whether it helped.", fixable)

    order = {"critical": 0, "warn": 1, "info": 2, "good": 3}
    out.sort(key=lambda f: order[f["level"]])
    return out
