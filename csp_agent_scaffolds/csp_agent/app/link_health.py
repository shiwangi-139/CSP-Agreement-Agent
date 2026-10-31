"""
app/link_health.py
Links (dashboard: Links): every CSP's upload links and the DC master link,
how they are used and where people get stuck.

  - Journey, per CSP in the period: a link was sent (a sent message carrying
    an upload link), opened, a send was tried, it reached us, a document was
    accepted. CSPs who did each step, so the steps need not nest exactly.
  - Problems: failed sends (UPLOAD_FAILED, reported by the upload pages) and
    refused links (LINK_REFUSED), in plain words, with whose side the fault
    is on. The CSP / DC is told only what they can act on; the cause is here.
  - Patterns: what many problems have in common (several failures on our
    side close together, a web-server size limit, links closed after
    acceptance while a document is due again, links opened but never used).
  - Needs attention: CSPs still missing a document whose last link event was
    a problem with no upload since, or who opened their link 3+ times and
    never sent anything.
  - One CSP: each link it was given (made, sent, opened, used, refused,
    closed / expired and why) and the timeline of what happened.

A CSP has one live link at a time, so opens and uploads (which don't name
the link) belong to the link that was live then. Event times are local (the
database's clock); token and message times are UTC (the app's): everything
returned here is local.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .config import local_now
from .models import CSP, AgreementEvent, MasterLink, OutboundMessage, OutboundStatus, PortalToken

LINK_EVENTS = ["LINK_OPENED", "PORTAL_UPLOAD", "LINK_REFUSED", "UPLOAD_FAILED", "LINK_REQUEST", "DC_LINK_VIEW"]
OPENED_NOT_USED = 3                    # opens without a send that make a CSP "need attention"
OUR_BURST, OUR_BURST_MINUTES = 3, 30   # this many failures on our side this close together: a pattern

FAILED_WHAT = {
    "offline": "Phone had no internet",
    "dropped": "Connection broke while sending (usually the mobile network)",
    "too_big": "Web server refused the file size (check its upload limit)",
    "slow": "Server too slow: the page stopped waiting",
    "server": "Server error or down",
    "page_error": "Error in the upload page itself",
}
REFUSED_WHAT = {
    "UNKNOWN": "Link not recognised (cut short when copied?)", "EXPIRED": "Link expired",
    "TOO_LONG": "Link damaged", "NO_TOKEN": "No link in the address", "CLOSED": "Link closed",
}
PLACE = {"page": "opening the page", "upload": "sending", "context": "loading", "question": "asking a question",
         "dc_lookup": "looking up a KO code"}
DOC_SECTION = {"agreement": "agreement", "pvr": "police certificate", "iibf": "IIBF"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _offset() -> timedelta:
    """Local time minus UTC, to the minute."""
    return timedelta(minutes=round((local_now() - _utcnow()).total_seconds() / 60))


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v else None


def device(ua: str) -> str:
    os_ = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("Windows", "Windows"),
                               ("Mac OS", "Mac")) if k in ua), "")
    br = next((n for k, n in (("SamsungBrowser", "Samsung"), ("Firefox", "Firefox"), ("Edg", "Edge"),
                              ("Chrome", "Chrome"), ("Safari", "Safari")) if k in ua), "")
    return " · ".join(x for x in (os_, br) if x)


def describe(e: AgreementEvent) -> tuple[str, str, str]:
    """A failed send or refused link in plain words: (side, what, details).
    Side is OUR, CSP or LINK."""
    p = e.payload or {}
    if e.event_type == "UPLOAD_FAILED":
        details = [PLACE.get(p.get("where"), p.get("where") or ""),
                   ", ".join(DOC_SECTION.get(s, s) for s in p.get("sections") or []),
                   f"{p['mb']} MB" if p.get("mb") else "", f"after {p['seconds']} s" if p.get("seconds") else "",
                   f"HTTP {p['status']}" if p.get("status") else "",
                   f"reported {p['late_min']} min later" if p.get("late_min") else "", device(p.get("browser") or "")]
        return p.get("side", "OUR"), FAILED_WHAT.get(p.get("kind"), p.get("kind") or "?"), \
            " · ".join(d for d in details if d)
    what = REFUSED_WHAT.get(p.get("reason"), p.get("reason") or "?")
    if p.get("closed_because") == "ALL_REQUESTED_DOCUMENTS_ACCEPTED":
        what = "Link closed: all its documents were accepted"
    elif e.source == "DC_MASTER_LINK" and p.get("reason") == "CLOSED":
        what = "DC link replaced or switched off"
    details = [f"while {PLACE[p['where']]}" if p.get("where") in PLACE else "",
               f"link {p['link']}…" if p.get("link") else ""]
    return "LINK", what, " · ".join(d for d in details if d)


def _clicks(e: AgreementEvent) -> int:
    return int((e.payload or {}).get("clicks") or 1)


def _accepted(e: AgreementEvent) -> bool:
    return any(r.get("ok") for r in (e.payload or {}).get("results") or [])


def _events(db: Session, ids: Optional[set[int]], since_local: datetime, types=LINK_EVENTS):
    q = db.query(AgreementEvent).filter(AgreementEvent.event_type.in_(types), AgreementEvent.sent_at >= since_local)
    if ids is not None:
        q = q.filter(AgreementEvent.csp_id.in_(ids or {-1}))
    return q.order_by(AgreementEvent.sent_at).all()


def problems(db: Session, since_local: datetime, ids: Optional[set[int]] = None, limit: int = 60) -> tuple[list, dict]:
    """Failed sends and refused links, newest first (at most `limit`), and
    how many on each side. `ids`: only these CSPs (None: all, with the DC
    link's and unknown links' too)."""
    events = _events(db, ids, since_local, ["UPLOAD_FAILED", "LINK_REFUSED"])[::-1][:2000]
    names = {c.id: (c.current_code, c.name) for c in db.query(CSP).filter(
        CSP.id.in_({e.csp_id for e in events if e.csp_id} or {-1}))}
    by_side, rows = Counter(), []
    for e in events:
        side, what, details = describe(e)
        by_side[side] += 1
        if len(rows) < limit:
            code, name = names.get(e.csp_id, (None, None))
            rows.append({"at": _iso(e.sent_at), "side": side, "what": what, "details": details,
                         "via": "DC link" if e.source == "DC_MASTER_LINK" else "CSP's link",
                         "csp": {"id": e.csp_id, "code": code, "name": name} if code else None})
    return rows, dict(by_side)


def _sent_links(db: Session, ids: Optional[set[int]], since_utc: datetime):
    """(csp_id, sent_at UTC, link) of messages sent to CSPs carrying an upload link."""
    q = db.query(OutboundMessage.csp_id, OutboundMessage.sent_at, OutboundMessage.payload_json["link"].astext).filter(
        OutboundMessage.status == OutboundStatus.SENT, OutboundMessage.sent_at >= since_utc,
        OutboundMessage.csp_id.isnot(None), OutboundMessage.payload_json["link"].astext.like("%token=%"))
    if ids is not None:
        q = q.filter(OutboundMessage.csp_id.in_(ids or {-1}))
    return q.all()


def overview(db: Session, ids: Optional[set[int]], days: int = 7, admin: bool = False) -> dict:
    """The Links page for these CSPs (an RM's own, or everyone's for an admin)."""
    days = min(max(days, 1), 90)
    now_local = local_now()
    since_local, since_utc = now_local - timedelta(days=days), _utcnow() - timedelta(days=days)
    events = _events(db, ids, since_local)
    by = defaultdict(lambda: defaultdict(list))                    # csp -> type -> [event]
    for e in events:
        if e.csp_id:
            by[e.csp_id][e.event_type].append(e)
    sent = _sent_links(db, ids, since_utc)
    tok = db.query(PortalToken)
    if ids is not None:
        tok = tok.filter(PortalToken.csp_id.in_(ids or {-1}))
    now = _utcnow()

    def did(kind, test=lambda e: True):
        return {c for c, t in by.items() if any(test(e) for e in t.get(kind, []))}
    opened = did("LINK_OPENED")
    reached = did("PORTAL_UPLOAD", lambda e: e.source != "DC_MASTER_LINK")
    tried = reached | did("UPLOAD_FAILED", lambda e: (e.payload or {}).get("where", "upload") == "upload"
                          and e.source != "DC_MASTER_LINK")
    accepted = did("PORTAL_UPLOAD", lambda e: e.source != "DC_MASTER_LINK" and _accepted(e))
    sent_to = {c for c, _, _ in sent}
    journey = [["Link sent", len(sent_to)], ["Opened", len(opened)], ["Tried to send", len(tried)],
               ["Reached us", len(reached)], ["A document accepted", len(accepted)]]

    rows, by_side = problems(db, since_local, ids if not admin else None)
    fails = [e for e in events if e.event_type == "UPLOAD_FAILED"] if not admin else \
        _events(db, None, since_local, ["UPLOAD_FAILED"])
    refusals = [e for e in events if e.event_type == "LINK_REFUSED"] if not admin else \
        _events(db, None, since_local, ["LINK_REFUSED"])
    kinds = Counter((e.payload or {}).get("kind") for e in fails)
    reasons = Counter(describe(e)[1] for e in refusals)

    csps = {c.id: c for c in db.query(CSP).filter(CSP.id.in_(set(by) or {-1}))}
    needs = {cid for cid, c in csps.items() if c.category in (2, 3, 4)}
    attention = []
    for cid in needs:
        t, c = by[cid], csps[cid]
        last_up = max((e.sent_at for e in t.get("PORTAL_UPLOAD", [])), default=None)
        bad = [e for e in t.get("LINK_REFUSED", []) + t.get("UPLOAD_FAILED", []) if last_up is None or e.sent_at > last_up]
        clicks = sum(_clicks(e) for e in t.get("LINK_OPENED", []))
        if bad:
            e = max(bad, key=lambda x: x.sent_at)
            side, what, _ = describe(e)
            nxt = {"LINK": "Send a new link", "OUR": "Check the server; ask them to try again",
                   "CSP": "Call: help with their internet"}[side]
            if side == "LINK" and (e.payload or {}).get("reason") == "UNKNOWN":
                nxt = "Send the link again (it may have been cut short)"
        elif clicks >= OPENED_NOT_USED and cid not in tried:
            e, side, what, nxt = max(t["LINK_OPENED"], key=lambda x: x.sent_at), "CSP", \
                f"Opened the link {clicks} times, never sent anything", "Call: help them upload"
        else:
            continue
        attention.append({"csp": {"id": cid, "code": c.current_code, "name": c.name}, "at": _iso(e.sent_at),
                          "side": side, "what": what, "next": nxt, "label": c.category})
    attention.sort(key=lambda r: r["at"], reverse=True)

    found = []
    ours = sorted(e.sent_at for e in fails if (e.payload or {}).get("side") == "OUR")
    for i in range(len(ours) - OUR_BURST + 1):
        if ours[i + OUR_BURST - 1] - ours[i] <= timedelta(minutes=OUR_BURST_MINUTES):
            j = i
            while j + 1 < len(ours) and ours[j + 1] - ours[i] <= timedelta(minutes=OUR_BURST_MINUTES):
                j += 1
            found.append({"side": "OUR", "title": f"{j - i + 1} failed sends on our side within {OUR_BURST_MINUTES} minutes",
                          "body": f"From {ours[i]:%d-%m %H:%M} to {ours[j]:%H:%M}. Several CSPs at once points to the "
                                  "server (slow or down), not their phones.", "at": _iso(ours[j])})
            break
    if kinds.get("too_big"):
        found.append({"side": "OUR", "title": f"The web server refused {kinds['too_big']} file(s) for size",
                      "body": "The page lets through files up to 10 MB, so the web server's own upload limit is "
                              "lower. Ask the server admin to raise it (client_max_body_size)."})
    closed_due = [e for e in refusals if (e.payload or {}).get("closed_because") == "ALL_REQUESTED_DOCUMENTS_ACCEPTED"
                  and e.csp_id in needs]
    if closed_due:
        found.append({"side": "LINK", "title": f"{len({e.csp_id for e in closed_due})} CSP(s) opened a link that "
                                               "closed after acceptance, while a document is due again",
                      "body": "Their old link closed when its documents were accepted. Send them a new link."})
    idle = [cid for cid in opened & needs if cid not in tried
            and sum(_clicks(e) for e in by[cid]["LINK_OPENED"]) >= 2]
    if idle:
        found.append({"side": "CSP", "title": f"{len(idle)} CSP(s) opened their link more than once but never sent",
                      "body": "They may not know what to upload or how. A call helps; so does a clearer page."})
    offline = kinds.get("offline", 0) + kinds.get("dropped", 0)
    if offline >= 5:
        found.append({"side": "CSP", "title": f"{offline} sends failed on the CSP's internet",
                      "body": "Weak mobile networks. Smaller files (photos made small on the phone) help."})

    out = {"days": days,
           "tiles": {"active_links": tok.filter(PortalToken.revoked_at.is_(None), PortalToken.expires_at > now).count(),
                     "links_made": tok.filter(PortalToken.created_at >= since_utc).count(),
                     "messages_with_link": len(sent),
                     "clicks": sum(_clicks(e) for e in events if e.event_type == "LINK_OPENED"),
                     "csps_opened": len(opened),
                     "success_rate": round(100 * len(accepted & opened) / len(opened)) if opened else None,
                     "refused_csps": len({e.csp_id for e in refusals if e.csp_id})},
           "journey": journey,
           "by_side": by_side,
           "failed_kinds": [[FAILED_WHAT.get(k, k or "?"), n, "bad" if k in ("too_big", "slow", "server", "page_error")
                             else "warn"] for k, n in kinds.most_common()],
           "refused_reasons": [[w, n, ""] for w, n in reasons.most_common()],
           "patterns": found, "attention": attention[:100], "problems": rows}
    if admin:
        out["dc_link"] = dc_link(db)
    return out


def dc_link(db: Session) -> dict:
    """The DC master link now and before: who made it, opens (CSPs looked
    up), uploads and refusals while it was the live one."""
    off = _offset()
    links = db.query(MasterLink).order_by(MasterLink.created_at.desc()).limit(10).all()
    rows = []
    for i, m in enumerate(links):
        start = m.created_at + off
        end = (m.revoked_at + off) if m.revoked_at else None
        q = db.query(AgreementEvent).filter(AgreementEvent.source == "DC_MASTER_LINK", AgreementEvent.sent_at >= start)
        if end:
            q = q.filter(AgreementEvent.sent_at < end)
        ev = q.all()
        rows.append({"made_at": _iso(start), "made_by": m.created_by, "closed_at": _iso(end), "active": end is None,
                     "lookups": sum(_clicks(e) for e in ev if e.event_type == "DC_LINK_VIEW"),
                     "uploads": sum(1 for e in ev if e.event_type == "PORTAL_UPLOAD"),
                     "failed": sum(1 for e in ev if e.event_type == "UPLOAD_FAILED"),
                     "refused": db.query(AgreementEvent).filter(
                         AgreementEvent.event_type == "LINK_REFUSED", AgreementEvent.source == "DC_MASTER_LINK",
                         AgreementEvent.payload["link"].astext == m.token_hash[:6]).count()})
    return {"links": rows}


def csp_links(db: Session, csp: CSP) -> dict:
    """One CSP: every upload link it was given and what happened on each,
    and the timeline of link events and messages carrying a link."""
    off = _offset()
    toks = db.query(PortalToken).filter(PortalToken.csp_id == csp.id).order_by(PortalToken.created_at).all()
    events = db.query(AgreementEvent).filter(AgreementEvent.csp_id == csp.id,
                                             AgreementEvent.event_type.in_(LINK_EVENTS)).order_by(AgreementEvent.sent_at).all()
    msgs = db.query(OutboundMessage.sent_at, OutboundMessage.payload_json["link"].astext, OutboundMessage.channel,
                    OutboundMessage.template_name).filter(
        OutboundMessage.csp_id == csp.id, OutboundMessage.status == OutboundStatus.SENT,
        OutboundMessage.payload_json["link"].astext.like("%token=%")).all()
    now = _utcnow()
    links = []
    for i, t in enumerate(toks):
        start = t.created_at + off
        nxt = toks[i + 1].created_at + off if i + 1 < len(toks) else None
        stop = min(x for x in (t.revoked_at and t.revoked_at + off, t.expires_at + off, nxt, now + off) if x)
        inside = [e for e in events if start <= e.sent_at < stop and e.source != "DC_MASTER_LINK"]
        if t.revoked_at:
            state = ("Closed: all its documents were accepted" if t.revoke_reason == "ALL_REQUESTED_DOCUMENTS_ACCEPTED"
                     else f"Closed ({(t.revoke_reason or 'no reason').replace('_', ' ').lower()})")
        elif t.expires_at <= now:
            state = "Expired"
        else:
            state = "Active"
        links.append({"ref": t.jti[:6], "made_at": _iso(start), "state": state, "active": state == "Active",
                      "closed_at": _iso(t.revoked_at + off if t.revoked_at else None),
                      "expires_at": _iso(t.expires_at + off), "asks_for": t.requested_types or [],
                      "sent": sum(1 for _, link, _, _ in msgs if link and f"token={t.jti}" in link),
                      "clicks": sum(_clicks(e) for e in inside if e.event_type == "LINK_OPENED"),
                      "uploads": sum(1 for e in inside if e.event_type == "PORTAL_UPLOAD"),
                      "failed": sum(1 for e in inside if e.event_type == "UPLOAD_FAILED"),
                      "refused": sum(1 for e in events if e.event_type == "LINK_REFUSED"
                                     and (e.payload or {}).get("link") == t.jti[:6])})
    timeline = []
    for e in events[-150:]:
        p = e.payload or {}
        if e.event_type in ("UPLOAD_FAILED", "LINK_REFUSED"):
            side, what, details = describe(e)
        elif e.event_type == "LINK_OPENED":
            side, what, details = "", "Opened the link", f"{_clicks(e)} clicks in that hour" if _clicks(e) > 1 else ""
        elif e.event_type == "DC_LINK_VIEW":
            side, what, details = "", "A DC looked up this CSP on the DC link", ""
        elif e.event_type == "LINK_REQUEST":
            side, what, details = "", "Asked for a new link on the get-link page", \
                "matched, link sent" if p.get("matched") and not p.get("limited") else "not sent"
        else:
            res = p.get("results") or []
            side = "DONE" if any(r.get("ok") for r in res) else ""
            what = ("Sent documents through the DC link" if e.source == "DC_MASTER_LINK" else "Sent documents")
            details = ", ".join(f"{DOC_SECTION.get(r.get('section'), r.get('section'))}: "
                                f"{'received' if r.get('ok') else (r.get('reason') or 'refused').replace('_', ' ')}"
                                for r in res)
        timeline.append({"at": _iso(e.sent_at), "side": side, "what": what, "details": details})
    for at, link, channel, template in msgs:
        if at:
            ref = link.split("token=", 1)[1][:6] if link and "token=" in link else ""
            timeline.append({"at": _iso(at + off), "side": "", "what": f"Link sent by {channel.title()}",
                             "details": f"{template.replace('_', ' ').lower()} · link {ref}…"})
    timeline = sorted(timeline, key=lambda r: r["at"], reverse=True)[:150]
    last = next((r for r in timeline if r["side"] in ("OUR", "CSP", "LINK")), None)
    return {"csp": {"id": csp.id, "code": csp.current_code, "name": csp.name, "label": csp.category},
            "links": links[::-1], "timeline": timeline, "last_problem": last}
