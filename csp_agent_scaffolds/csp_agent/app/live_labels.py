"""
Live Labels: a CSP's saved Label and sub-group (csp.category, sub_slab,
category_reason) are brought up to date in the SAME save as any change to
what they are worked out from: its documents, their Review queue items and
its PVR applications. So the dashboard (Labels tabs and counts, Overview,
Pending by RM, Find CSPs) changes the moment a document is accepted, from
every route (CSP link, DC link, common link, Gmail, Review queue, scripts),
and no route has to remember to call refresh_category itself.

The one change with no save behind it, a date passing (a document expiring,
or entering its last 60 days), is caught by the vault job at 00:10
(app/vault.py reconcile), which refreshes every CSP with documents.

Installed once for every session (SQLAlchemy events on the Session class),
by importing this module from app/models.py.
"""
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger("app.live_labels")
_KEY = "live_labels"            # session.info: {"csp": set of CSP ids, "doc": set of Document ids}
_RUNNING = "live_labels_running"
_MAX_ROUNDS = 3


def _pending(session: Session) -> dict:
    return session.info.setdefault(_KEY, {"csp": set(), "doc": set()})


@event.listens_for(Session, "before_flush")
def _note_changes(session: Session, flush_context, instances) -> None:
    from .models import Document, ManualReviewQueue, PvrApplication
    if session.info.get("live_labels_off"):
        return
    p = None
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, (Document, PvrApplication)):
            if obj in session.dirty and not session.is_modified(obj):
                continue
            if obj.csp_id is not None:
                p = p or _pending(session)
                p["csp"].add(obj.csp_id)
        elif isinstance(obj, ManualReviewQueue):
            if obj in session.dirty and not session.is_modified(obj):
                continue
            if obj.document_id is not None:
                p = p or _pending(session)
                p["doc"].add(obj.document_id)


@event.listens_for(Session, "before_commit")
def _refresh_labels(session: Session) -> None:
    if session.info.get("live_labels_off") or session.info.get(_RUNNING):
        return                                # (a savepoint below commits too: not again from inside)
    if _KEY not in session.info and not (session.new or session.dirty or session.deleted):
        return                                # nothing saved in this transaction touches a Label
    session.info[_RUNNING] = True
    try:
        _refresh_rounds(session)
    finally:
        session.info.pop(_RUNNING, None)
        session.info.pop(_KEY, None)


def _refresh_rounds(session: Session) -> None:
    from .compliance import refresh_category
    from .models import CSP, Document
    done = set()
    for _ in range(_MAX_ROUNDS):
        session.flush()                       # notes this save's changes (and the queries below see them)
        p = session.info.pop(_KEY, None)
        if not p:
            break
        ids = set(p["csp"])
        if p["doc"]:
            ids |= {cid for (cid,) in session.query(Document.csp_id).filter(Document.id.in_(p["doc"]))
                    if cid is not None}
        ids -= done                           # a second round is only this refresh's own status changes
        if not ids:
            break
        for csp in session.query(CSP).filter(CSP.id.in_(ids)):
            try:
                with session.begin_nested():
                    refresh_category(session, csp)
            except Exception:                 # a Label is never a reason to lose the upload itself
                logger.exception("live_label_failed csp_id=%s", csp.id)
        done |= ids


@event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop(_KEY, None)
