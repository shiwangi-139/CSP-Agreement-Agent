"""
app/comms/upload_receipt.py
The friendly WhatsApp a CSP gets right after their documents are handled:

  UPLOAD_SUCCESS_ALL  all three documents are on record: nothing more to do
  UPLOAD_RECEIPT      what was recorded, what our team is checking, and what
                      was NOT recorded (with the reason) + the link to redo it

Sent automatically (no approval: the CSP has just acted) after an upload on
the upload page, after a re-check (scripts/recheck_rejected_uploads.py) and
after a reviewer's decision. Every one is an OutboundMessage, so it is on
the dashboard (Communication Hub) and in the database like any other
message. Help lines name the DC first, then the RM.
"""
import logging
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from ..compliance import DOC_LABELS, evaluate
from ..models import CSP, InternalUser, OutboundStatus

logger = logging.getLogger(__name__)

SECTION_TYPE = {"agreement": "AGREEMENT", "pvr": "POLICE_VERIFICATION", "iibf": "IIBF_CERTIFICATE"}
RECORDED = {"accepted", "duplicate", "review"}          # "review": recorded, typed date differs (team checks the date)
CHECKING = {"received_check"}
# Short reasons for the message (the upload page shows the full text).
SHORT_REASON = {
    "wrong_doc": ("यह सही डॉक्यूमेंट नहीं है", "not the right document"),
    "expired": ("इसकी तारीख निकल चुकी है", "it has expired"),
    "unreadable": ("साफ़ पढ़ा नहीं जा सका", "could not be read clearly"),
    "too_small": ("फोटो बहुत छोटी है", "photo too small"),
    "too_dark": ("फोटो बहुत अंधेरी है", "photo too dark"),
    "washed_out": ("फोटो में अक्षर नहीं दिख रहे", "text not visible"),
    "blurry": ("फोटो धुंधली है", "photo blurry"),
    "no_date": ("तारीख नहीं भरी", "no issue date entered"),
    "future": ("तारीख आज के बाद की है", "date in the future"),
    "too_big": ("फ़ाइल बहुत बड़ी है", "file too large"),
    "file_type": ("सिर्फ़ PDF/JPG/PNG चलेगी", "only PDF/JPG/PNG"),
    "rejected_by_team": ("हमारी टीम ने जाँच में सही नहीं पाया", "our team found it was not correct"),
}
NONE_HI, NONE_EN = "कोई नहीं", "none"


def _staff_text(db: Session, uid: Optional[int], role: str) -> str:
    u = db.get(InternalUser, uid) if uid else None
    if u is None:
        return f"आपके {role} / your {role}"
    return f"{role} {u.name}" + (f" ({u.phone})" if u.phone else "")


def help_values(db: Session, csp: CSP) -> tuple[str, str]:
    """(DC text, RM text): CSPs should call their DC first, then their RM."""
    return _staff_text(db, csp.dc_id, "DC"), _staff_text(db, csp.rm_id, "RM")


def help_line(db: Session, csp: CSP) -> str:
    """One line for message layouts (no line breaks: Meta allows none in a value)."""
    dc, rm = help_values(db, csp)
    return f"पहले {dc}, फिर {rm} / First {dc}, then {rm}"


def summarize(results: list[dict]) -> dict:
    """Recorded / being checked / not recorded, in Hindi and English, from
    the upload page's per-section results."""
    ok_t, check_t, bad = [], [], []
    for r in results:
        t = SECTION_TYPE.get(r.get("section"), r.get("type"))
        if t not in DOC_LABELS:
            continue
        reason = r.get("reason") or ("accepted" if r.get("ok") else "wrong_doc")
        if r.get("ok") and reason in CHECKING:
            check_t.append(t)
        elif r.get("ok"):
            ok_t.append(t)
        else:
            bad.append((t, reason))

    def names(types, i):
        return ", ".join(DOC_LABELS[t][i] for t in dict.fromkeys(types)) or (NONE_EN if i == 0 else NONE_HI)

    def bads(i):
        if not bad:
            return NONE_EN if i == 0 else NONE_HI
        lang = 1 if i == 1 else 0
        return " · ".join(f"{DOC_LABELS[t][i]} ({SHORT_REASON.get(reason, SHORT_REASON['wrong_doc'])[0 if lang else 1]})"
                          for t, reason in bad)
    return {"ok_hi": names(ok_t, 1), "ok_en": names(ok_t, 0), "check_hi": names(check_t, 1),
            "check_en": names(check_t, 0), "bad_hi": bads(1), "bad_en": bads(0), "any_bad": bool(bad)}


def send_upload_receipt(csp_id: int, results: Optional[list[dict]] = None, session_factory=None) -> Optional[str]:
    """Send the right message for this CSP now: "all done" when nothing is
    needed any more, else the receipt for `results`. Uses its own session
    (it runs in the background). Returns the message status, or None."""
    from ..db import SessionLocal
    from ..portal_tokens import issue_upload_link
    from . import outbound
    db = (session_factory or SessionLocal)()
    try:
        csp = db.get(CSP, csp_id)
        if csp is None:
            return None
        state = evaluate(db, csp)
        dc, rm = help_values(db, csp)
        if not state.needs_upload:
            key, values, ctx_extra = "UPLOAD_SUCCESS_ALL", {}, {}
        elif results:
            key, values = "UPLOAD_RECEIPT", summarize(results)
            values.pop("any_bad")
            ctx_extra = dict(values)
        else:
            return None
        link = issue_upload_link(db, csp, state.needs_upload) if state.needs_upload else None
        ctx = {"csp_name": csp.name, "csp_code": csp.current_code, "docs": [], "upload_link": link,
               "rm_name": None, "dc_help": dc, "rm_help": rm, **ctx_extra}
        drafted = outbound.draft(db, csp=csp, role="CSP", template_key=key, ctx=ctx,
                                 key_base=f"{key}:{csp.id}:{uuid.uuid4().hex[:12]}", channels=("WHATSAPP",),
                                 stage="AFTER_UPLOAD")
        m = drafted[0]
        # The layout's other values; its help line (DC first, then RM) is
        # filled in at send time like every other message.
        m.payload_json = {**(m.payload_json or {}), "layout_values": values}
        if m.status != OutboundStatus.BLOCKED:
            # The CSP has just acted: this reply goes out at once, no approval
            # (still only to a number on the calling sheet: outbound.send checks).
            m.status, m.reviewed_by, m.reviewed_at = OutboundStatus.APPROVED, "agent (reply to upload)", outbound._now()
            outbound.send(db, m)
        db.commit()
        logger.info("upload_receipt csp_id=%s template=%s status=%s", csp_id, key, m.status.value)
        return m.status.value
    except Exception:
        db.rollback()
        logger.exception("upload_receipt_failed csp_id=%s", csp_id)
        return None
    finally:
        db.close()
