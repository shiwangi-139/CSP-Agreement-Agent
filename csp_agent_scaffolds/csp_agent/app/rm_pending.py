"""
app/rm_pending.py
Pending list per RM (dashboard: "Pending by RM", and Find CSPs -> "Pending
list by RM"): their working CSPs in Labels 2, 3 and 4 (a document not on file
yet) and in Label 1 with a document expired (1.3), district by district, with the DC, each document in plain words and
what to upload. Shown as a table (pending_rows) and as one CSV per RM, both
made live.

District: the calling sheet's "District" column when it has one, else read
from the CSP's full address ("Dist. Patna", "District: Gaya", "जिला ..."),
else "District not given".
"""
import csv
import io
import re
from datetime import date
from typing import Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from .compliance import DOC_LABELS, REQUIRED_TYPES, evaluate, slab_label, sub_slab, sub_slab_label
from .models import CSP, InternalUser

LABELS = (2, 3, 4)
NO_DISTRICT = "District not given"
# "Dist. Patna", "Distt-Gaya", "District: Nalanda", "जिला पटना" up to the next comma, digit or PIN.
_DISTRICT = re.compile(r"(?:\bdist(?:rict|t)?\b\.?|जिला|जि\.)\s*[:\-–.]?\s*"
                       r"([A-Za-zऀ-ॿ][A-Za-zऀ-ॿ .]{1,30}?)\s*(?=[,\-–(/|]|\d|\bpin\b|\bstate\b|$)", re.I)


def district_from_address(address: Optional[str]) -> Optional[str]:
    m = _DISTRICT.search(address or "")
    if not m:
        return None
    return _clean(m.group(1))


def _clean(name: Optional[str]) -> Optional[str]:
    """One spelling per district: spaces tidied, "SITAMARHI" / "sitamarhi" -> "Sitamarhi"."""
    name = re.sub(r"\s+", " ", (name or "").strip(" .,-"))
    if not name:
        return None
    return name.title() if name.isascii() else name


def district_of(csp: CSP) -> str:
    return _clean(csp.district) or _clean(district_from_address(csp.kiosk_location)) or NO_DISTRICT


def _csps(db: Session, rm_id: Optional[int] = None):
    """The list's CSPs: one RM's, or (rm_id None) everyone's, CSPs with no RM included."""
    # Label 1 with an expired document: the stored sub-group or reason says so
    # (pending_rows checks it again with today's evaluation).
    expired_1 = and_(CSP.category == 1, or_(CSP.sub_slab == "1.3", CSP.category_reason.ilike("%Expired:%")))
    q = db.query(CSP).filter(or_(CSP.category.in_(LABELS), expired_1), CSP.is_active_in_calling_sheet.is_(True),
                             or_(CSP.state.is_(None), CSP.state != "TEST"))
    return (q if rm_id is None else q.filter(CSP.rm_id == rm_id)).all()


def rm_list(db: Session, rm_ids: Optional[set[int]] = None) -> list[dict]:
    """Each RM with how many of their CSPs are on the list."""
    q = db.query(InternalUser).filter(InternalUser.role == "RM")
    if rm_ids is not None:
        q = q.filter(InternalUser.id.in_(rm_ids or {-1}))
    return [{"id": u.id, "name": u.name, "csps": len(_csps(db, u.id))} for u in q.order_by(InternalUser.name)]


def _word(t: str, s) -> str:
    fmt = lambda d: d.strftime("%d-%m-%Y")
    if s.in_review:
        return "Being checked by Eko"
    if s.status == "VALID":
        if s.expiry_date is None:
            return "Submitted"
        return f"OK till {fmt(s.expiry_date)}"
    if s.status == "EXPIRED":
        return f"Expired on {fmt(s.expiry_date)}" if s.expiry_date else "Expired"
    if s.status == "UNREADABLE":
        return "Not readable – send again"
    return "Pending"


def _tone(s) -> str:
    """How a document's word is shown on the dashboard: ok / warn / bad."""
    if s.in_review:
        return "warn"
    return {"VALID": "ok", "EXPIRED": "bad", "UNREADABLE": "bad"}.get(s.status, "warn")


def pending_rows(db: Session, rm_id: Optional[int] = None, today: Optional[date] = None) -> list[dict]:
    """The list as rows for the dashboard, district by district, then DC and CSP code."""
    today = today or date.today()
    staff = {u.id: u for u in db.query(InternalUser).filter(InternalUser.role.in_(("RM", "DC")))}
    name = lambda i, none: staff[i].name if i in staff else none
    rows = []
    for c in _csps(db, rm_id):
        st = evaluate(db, c, today)
        if st.category == 1 and not st.expired:
            continue
        rows.append({
            "id": c.id, "district": district_of(c), "state": _clean(c.state) or "", "rm_id": c.rm_id,
            "rm": name(c.rm_id, "No RM"), "dc": name(c.dc_id, "No DC"), "code": c.current_code, "name": c.name,
            "phone": c.phone or "", "label": slab_label(st.category), "category": st.category,
            "sub_slab_label": sub_slab_label(sub_slab(st)),
            "docs": {t: {"word": _word(t, st.docs[t]), "tone": _tone(st.docs[t])} for t in REQUIRED_TYPES},
            "to_upload": [t for t in REQUIRED_TYPES
                          if not st.docs[t].in_review and st.docs[t].status in ("MISSING", "UNREADABLE", "EXPIRED")],
        })
    rows.sort(key=lambda r: (r["district"] == NO_DISTRICT, r["district"].lower(), r["dc"].lower(), r["code"] or ""))
    return rows


def rm_csv(db: Session, rm: InternalUser, today: Optional[date] = None) -> tuple[str, bytes]:
    """(file name, CSV bytes) for one RM. UTF-8 with a BOM so Excel shows Hindi names."""
    today = today or date.today()
    rows = [[r["district"], r["dc"], r["code"], r["name"], r["phone"], r["label"]]
            + [r["docs"][t]["word"] for t in REQUIRED_TYPES] + [", ".join(DOC_LABELS[t][0] for t in r["to_upload"])]
            for r in pending_rows(db, rm.id, today)]
    head = ["District", "DC", "CSP code", "CSP name", "Phone", "Label"] + [DOC_LABELS[t][0] for t in REQUIRED_TYPES] \
        + ["To upload"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    # One block per district: a blank line, "PATNA · 13 CSPs", then its CSPs by DC.
    for i, r in enumerate(rows):
        if i == 0 or r[0] != rows[i - 1][0]:
            n = sum(1 for x in rows if x[0] == r[0])
            if i:
                w.writerow([""] * len(head))
            w.writerow([f"{r[0].upper()} · {n} CSP{'s' if n != 1 else ''}"] + [""] * (len(head) - 1))
        w.writerow(r)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", rm.name or "RM").strip("_")
    return f"pending_{safe}_{today:%d-%m-%Y}.csv", buf.getvalue().encode("utf-8-sig")
