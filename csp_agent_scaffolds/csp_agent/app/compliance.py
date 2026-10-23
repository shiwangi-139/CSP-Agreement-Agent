"""
Compliance slabs, computed from each CSP's current documents and STORED on
the csp row so the dashboard and the engine read the same answer. A slab says
how many of the three documents are ON FILE (valid or expired):

  1 COMPLETE     all three on file
  2 MISSING_ONE  two on file, one missing
  3 MISSING_TWO  one on file, two missing
  4 NONE         nothing readable on file from the last 2 years

Expiry is shown inside every slab (ComplianceState.expired): a CSP in any
slab can have expired documents, and its messages ask to renew those as well
as to upload the missing ones. Moving between slabs needs only an upload.
IIBF never expires. Unreadable files are kept for audit but count as missing.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus

REQUIRED_TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")
TYPE_ALIASES = {
    "CHARACTER_CERTIFICATE": "POLICE_VERIFICATION",
    "PVR": "POLICE_VERIFICATION",
    "IIBF_CERTIFICATION": "IIBF_CERTIFICATE",
    "CSP_AGREEMENT": "AGREEMENT",
}
# Codes used by the API and the WhatsApp agent: keep them stable.
CATEGORY_NAMES = {1: "COMPLETE", 2: "MISSING_ONE", 3: "MISSING_TWO", 4: "NONE"}
# What people see ("Cat A/B/1/2" is already used for other things in the company).
SLAB_NAMES = {1: "All documents on file", 2: "1 document missing", 3: "2 documents missing", 4: "No documents"}


def slab_label(category: int) -> str:
    """e.g. "Label 3 · 2 documents missing". Users see "Label": "slab" already
    means something else on Eko's website (internally the code keeps "slab")."""
    return f"Label {category} · {SLAB_NAMES.get(category, '')}"


# Sub-groups inside each slab: what exactly is on file, so each group needs
# one clear action. Numbered 1.1, 1.2 ... (letters would look like CSP codes
# 1A85... and the old "Cat A"). Expired / unreachable / no RM are TAGS on top.
SUB_SLABS = {
    "1.1": "Fully compliant",
    "1.2": "Renewal coming up (60 days)",
    "1.3": "Renewal overdue (expired)",
    "2.1": "Agreement missing",
    "2.2": "PVR missing",
    "2.3": "IIBF missing",
    "3.1": "Only Agreement on file",
    "3.2": "Only PVR on file",
    "3.3": "Only IIBF on file",
    "4.1": "Never sent anything",
    "4.2": "Sent, but unreadable",
}
RENEWAL_SOON_DAYS = 60
_ORDER = {"AGREEMENT": "1", "POLICE_VERIFICATION": "2", "IIBF_CERTIFICATE": "3"}


def sub_slab(state: "ComplianceState") -> str:
    present = [t for t, s in state.docs.items() if s.status in ("VALID", "EXPIRED")]
    if state.category == 1:
        if state.expired:
            return "1.3"
        soon = any(s.status == "VALID" and s.days_left is not None and s.days_left <= RENEWAL_SOON_DAYS
                   for s in state.docs.values())
        return "1.2" if soon else "1.1"
    if state.category == 2:
        return "2." + _ORDER[state.missing[0]]
    if state.category == 3:
        return "3." + _ORDER[present[0]]
    return "4.2" if any(s.status == "UNREADABLE" for s in state.docs.values()) else "4.1"


def sub_slab_label(code: Optional[str]) -> str:
    return f"{code} · {SUB_SLABS[code]}" if code in SUB_SLABS else ""
DOC_LABELS = {
    "AGREEMENT": ("CSP Agreement", "सीएसपी एग्रीमेंट"),
    "POLICE_VERIFICATION": ("Police Verification / Character Certificate", "पुलिस वेरिफिकेशन / चरित्र प्रमाण पत्र"),
    "IIBF_CERTIFICATE": ("IIBF Certificate", "आईआईबीएफ सर्टिफिकेट"),
}
# Statuses that mean "we have a usable copy of this document".
USABLE = {DocumentStatus.VALID, DocumentStatus.NEEDS_APPROVAL, DocumentStatus.MANUAL_VERIFIED,
          DocumentStatus.EXPIRED, DocumentStatus.EXTRACTED_DETERMINISTIC, DocumentStatus.EXTRACTED_AI,
          DocumentStatus.NEEDS_REVIEW}


INACTIVE_LABEL = 5          # shown as "Inactive"; Labels 1-4 are for working terminals


def is_inactive(csp: CSP) -> bool:
    """The calling sheet's Terminal Status says the terminal is not working."""
    from .config import INACTIVE_TERMINAL_STATUSES
    return " ".join((csp.terminal_status or "").lower().split()) in INACTIVE_TERMINAL_STATUSES


def inactive_clause():
    """SQL form of is_inactive(), for queries."""
    from .config import INACTIVE_TERMINAL_STATUSES
    return func.lower(func.trim(func.coalesce(CSP.terminal_status, ""))).in_(INACTIVE_TERMINAL_STATUSES)


def working_clause():
    """CSPs on the calling sheet whose terminal is working (Labels 1-4)."""
    return CSP.is_active_in_calling_sheet.is_(True) & ~inactive_clause()


def canonical_type(doc_type: Optional[str]) -> Optional[str]:
    t = (doc_type or "").upper()
    return TYPE_ALIASES.get(t, t) if t else None


@dataclass
class DocState:
    document_type: str
    status: str                  # VALID, EXPIRED, MISSING, UNREADABLE
    document: Optional[Document] = None
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    days_left: Optional[int] = None
    # The current copy waits for a person (Review queue): no "renew / upload
    # again" reminder for it until they decide.
    in_review: bool = False
    # Police certificate only: the CSP applied for a new one on this date
    # (PvrApplication), newer than the copy on record. Doesn't count.
    applied_on: Optional[date] = None

    @property
    def shown_status(self) -> str:
        """What the CSP sees: a copy a person is still checking is "being
        checked", never "expired" or "valid" yet."""
        return "IN_REVIEW" if self.in_review else self.status


@dataclass
class ComplianceState:
    category: int
    reason: str
    docs: dict = field(default_factory=dict)  # type -> DocState

    @property
    def missing(self) -> list[str]:
        return [t for t, s in self.docs.items() if s.status in ("MISSING", "UNREADABLE")]

    @property
    def expired(self) -> list[str]:
        return [t for t, s in self.docs.items() if s.status == "EXPIRED" and not s.in_review]

    @property
    def needs_upload(self) -> list[str]:
        """What the CSP is asked to send. A type with a copy waiting in the
        Review queue is not asked for again until a person decides."""
        return [t for t in self.expired + self.missing if not self.docs[t].in_review]


def latest_documents(db: Session, csp_id: int) -> dict[str, Document]:
    """The current usable document per type. Which copy is current is decided
    in one place, document_service.recompute_current; if two rows are still
    flagged current (old data), the newest by issue date wins."""
    out: dict[str, Document] = {}
    docs = db.query(Document).filter(Document.csp_id == csp_id, Document.is_current.is_(True)).all()
    for d in docs:
        t = canonical_type(d.document_type)
        if t not in REQUIRED_TYPES or d.status not in USABLE or d.readability == "UNREADABLE":
            continue
        best = out.get(t)
        key = (d.issue_date or date.min, d.uploaded_at or datetime.min)
        if best is None or key > (best.issue_date or date.min, best.uploaded_at or datetime.min):
            out[t] = d
    return out


def evaluate(db: Session, csp: CSP, today: Optional[date] = None) -> ComplianceState:
    today = today or date.today()
    latest = latest_documents(db, csp.id)
    unreadable_types = {
        canonical_type(t) for (t,) in db.query(Document.document_type).filter(
            Document.csp_id == csp.id, Document.readability == "UNREADABLE")
    }
    # Types with an upload waiting for a person (it doesn't count yet, but the
    # CSP did send it: no "please upload" reminder meanwhile).
    waiting = {canonical_type(t) for (t,) in db.query(Document.document_type)
               .join(ManualReviewQueue, ManualReviewQueue.document_id == Document.id)
               .filter(Document.csp_id == csp.id, ManualReviewQueue.status == ReviewStatus.PENDING,
                       Document.status == DocumentStatus.NEEDS_REVIEW)}
    docs = {}
    for t in REQUIRED_TYPES:
        d = latest.get(t)
        if d is None:
            docs[t] = DocState(t, "UNREADABLE" if t in unreadable_types else "MISSING", in_review=t in waiting)
            continue
        exp = d.expiry_date
        expired = exp is not None and exp < today
        docs[t] = DocState(t, "EXPIRED" if expired else "VALID", d, d.issue_date, exp,
                           (exp - today).days if exp else None,
                           in_review=d.status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.NEEDS_APPROVAL)
                           or (expired and t in waiting))

    present = [t for t, s in docs.items() if s.status in ("VALID", "EXPIRED")]
    expired = [t for t, s in docs.items() if s.status == "EXPIRED" and not s.in_review]
    missing = [t for t, s in docs.items() if s.status in ("MISSING", "UNREADABLE")]
    cat = {3: 1, 2: 2, 1: 3, 0: 4}[len(present)]
    parts = []
    if missing:
        parts.append("Missing: " + ", ".join(
            DOC_LABELS[t][0] + (" (unreadable copy received)" if docs[t].status == "UNREADABLE" else "")
            for t in missing))
    if expired:
        parts.append("Expired: " + ", ".join(DOC_LABELS[t][0] for t in expired))
    if not present:
        reason = "No readable documents on file from the last 2 years."
    elif not parts:
        reason = "All three documents valid."
    else:
        reason = "; ".join(parts)
    from .models import PvrApplication
    app = (db.query(PvrApplication).filter(PvrApplication.csp_id == csp.id)
           .order_by(PvrApplication.created_at.desc()).first())
    pvr = docs["POLICE_VERIFICATION"]
    if app is not None and (pvr.document is None or pvr.document.uploaded_at is None
                            or app.created_at > pvr.document.uploaded_at):
        pvr.applied_on = app.applied_on
    return ComplianceState(cat, reason, docs)


def refresh_category(db: Session, csp: CSP, today: Optional[date] = None) -> ComplianceState:
    state = evaluate(db, csp, today)
    sub = sub_slab(state)
    if csp.sub_slab != sub:
        csp.sub_slab = sub
    if csp.category != state.category or csp.category_reason != state.reason:
        csp.category = state.category
        csp.category_reason = state.reason[:500]
        csp.category_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # Keep document statuses honest as time passes.
    for s in state.docs.values():
        if s.document is not None and s.status == "EXPIRED" and s.document.status in (
                DocumentStatus.VALID, DocumentStatus.MANUAL_VERIFIED):
            s.document.status = DocumentStatus.EXPIRED
    return state


def refresh_all(db: Session, today: Optional[date] = None) -> dict[int, int]:
    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    for csp in db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)):
        counts[refresh_category(db, csp, today).category] += 1
    return counts
