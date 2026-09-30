"""
Compliance categories, computed from each CSP's current documents and
STORED on the csp row so the dashboard and the engine read the same answer.

  1 ACTIVE   all three documents on file and none expired
  2 PARTIAL  at least one on file and none expired, but some missing
             (never received, or received but unreadable)
  3 EXPIRED  at least one on file, and at least one of them expired
             (others may be missing too)
  4 NONE     nothing readable on file from the last 2 years

IIBF never expires. Unreadable files are kept for audit but count as missing.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import CSP, Document, DocumentStatus

REQUIRED_TYPES = ("AGREEMENT", "POLICE_VERIFICATION", "IIBF_CERTIFICATE")
TYPE_ALIASES = {
    "CHARACTER_CERTIFICATE": "POLICE_VERIFICATION",
    "PVR": "POLICE_VERIFICATION",
    "IIBF_CERTIFICATION": "IIBF_CERTIFICATE",
    "CSP_AGREEMENT": "AGREEMENT",
}
# Codes used by the API and the WhatsApp agent: keep them stable.
CATEGORY_NAMES = {1: "ACTIVE", 2: "PARTIAL", 3: "EXPIRED", 4: "NONE"}
# What people see ("Cat A/B/1/2" is already used for other things in the company).
SLAB_NAMES = {1: "Compliant", 2: "Documents missing", 3: "Renewal due", 4: "No documents"}


def slab_label(category: int) -> str:
    """e.g. "Slab 3 · Renewal due"."""
    return f"Slab {category} · {SLAB_NAMES.get(category, '')}"
DOC_LABELS = {
    "AGREEMENT": ("CSP Agreement", "सीएसपी एग्रीमेंट"),
    "POLICE_VERIFICATION": ("Police Verification / Character Certificate", "पुलिस वेरिफिकेशन / चरित्र प्रमाण पत्र"),
    "IIBF_CERTIFICATE": ("IIBF Certificate", "आईआईबीएफ प्रमाण पत्र"),
}
# Statuses that mean "we have a usable copy of this document".
USABLE = {DocumentStatus.VALID, DocumentStatus.NEEDS_APPROVAL, DocumentStatus.MANUAL_VERIFIED,
          DocumentStatus.EXPIRED, DocumentStatus.EXTRACTED_DETERMINISTIC, DocumentStatus.EXTRACTED_AI,
          DocumentStatus.NEEDS_REVIEW}


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
        return [t for t, s in self.docs.items() if s.status == "EXPIRED"]

    @property
    def needs_upload(self) -> list[str]:
        return self.expired + self.missing


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
    docs = {}
    for t in REQUIRED_TYPES:
        d = latest.get(t)
        if d is None:
            docs[t] = DocState(t, "UNREADABLE" if t in unreadable_types else "MISSING")
            continue
        exp = d.expiry_date
        expired = exp is not None and exp < today
        docs[t] = DocState(t, "EXPIRED" if expired else "VALID", d, d.issue_date, exp,
                           (exp - today).days if exp else None)

    present = [t for t, s in docs.items() if s.status in ("VALID", "EXPIRED")]
    expired = [t for t, s in docs.items() if s.status == "EXPIRED"]
    missing = [t for t, s in docs.items() if s.status in ("MISSING", "UNREADABLE")]
    if not present:
        cat, reason = 4, "No readable documents on file from the last 2 years."
    elif expired:
        cat, reason = 3, "Expired: " + ", ".join(DOC_LABELS[t][0] for t in expired) + (
            "; missing: " + ", ".join(DOC_LABELS[t][0] for t in missing) if missing else "")
    elif missing:
        cat, reason = 2, "Missing: " + ", ".join(
            DOC_LABELS[t][0] + (" (unreadable copy received)" if docs[t].status == "UNREADABLE" else "")
            for t in missing)
    else:
        cat, reason = 1, "All three documents valid."
    return ComplianceState(cat, reason, docs)


def refresh_category(db: Session, csp: CSP, today: Optional[date] = None) -> ComplianceState:
    state = evaluate(db, csp, today)
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
