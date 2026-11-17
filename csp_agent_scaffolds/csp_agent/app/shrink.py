"""
app/shrink.py
Smaller copies of large scanned documents, without changing what they say.

Agreements were 81% of the vault on 18-11-2026 (786 files, 5.1 GB, 6.5 MB
each on average): phone scans at full camera resolution. Each page of a
scanned PDF is drawn again at most SHRINK_LONG_SIDE pixels on its long side
(about 150 DPI on A4), IN COLOUR (stamps and signatures are checked), as a
JPEG of quality SHRINK_QUALITY, in a new PDF with the same page sizes.

A smaller copy is kept only when:
  - it is at least MIN_SAVING (30%) smaller, and
  - the reader gets exactly the same answers from it as from the original
    (type, issue date, expiry, validity rule, status), each read in its own
    process (scripts/check_all_documents.read_isolated).
Then the original is moved to _originals/<date>/ (kept ORIGINALS_KEEP_DAYS,
app/vault.py purge_originals) and the smaller copy takes its place under the
same name. Document.sha256 stays the hash of the file as RECEIVED (so the
same file sent again is still recognised); the new size and hash are noted
in extracted_fields["shrunk"].

Not touched: digital PDFs (real text on the page: small already, and read
best as they are), photos (JPG / PNG), files under SHRINK_MIN_MB.
scripts/shrink_documents.py runs it (a dry run by default); the worker runs
it every night only when SHRINK_NIGHTLY is on.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from . import vault
from .config import SHRINK_LONG_SIDE, SHRINK_MIN_MB, SHRINK_QUALITY, SHRINK_TYPES
from .models import AgreementEvent, CSP, Document

logger = logging.getLogger(__name__)

MIN_SAVING = 0.30
MAX_DPI = 300
TEXT_PAGE_CHARS = 200            # a page with this much real text is digital, not a scan
COMPARED = ("readability", "document_type", "start_date", "expiry_date", "validity_rule_used", "compliance_status")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_scanned_pdf(data: bytes) -> Optional[bool]:
    """True for a PDF made of page images, False for a digital one, None if not a PDF."""
    import fitz
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None
    try:
        if doc.page_count == 0:
            return None
        text_pages = sum(1 for p in doc if len(p.get_text("text").strip()) >= TEXT_PAGE_CHARS)
        return text_pages * 2 < doc.page_count          # most pages without real text
    finally:
        doc.close()


def shrink_pdf(data: bytes, long_side: int = SHRINK_LONG_SIDE, quality: int = SHRINK_QUALITY) -> Optional[bytes]:
    """The PDF with each page redrawn in colour at most `long_side` pixels;
    None when it would not be MIN_SAVING smaller (or can't be opened)."""
    import fitz
    try:
        src = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None
    out = fitz.open()
    try:
        for page in src:
            r = page.rect
            zoom = min(long_side / max(r.width, r.height), MAX_DPI / 72)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            new = out.new_page(width=r.width, height=r.height)
            new.insert_image(new.rect, stream=pix.tobytes("jpeg", jpg_quality=quality))
        small = out.tobytes(garbage=4, deflate=True)
    except Exception:
        logger.exception("shrink_failed")
        return None
    finally:
        out.close()
        src.close()
    return small if len(small) <= (1 - MIN_SAVING) * len(data) else None


def same_reading(a: dict, b: dict) -> list[str]:
    """The fields that differ between two readings (empty: the same)."""
    if a.get("error") or a.get("timeout") or b.get("error") or b.get("timeout"):
        return ["could not read"]
    return [k for k in COMPARED if str(a.get(k)) != str(b.get(k))]


def candidates(db: Session, types=SHRINK_TYPES, min_mb: float = SHRINK_MIN_MB, only: Optional[list[int]] = None):
    """(document, path, size) of files that may be made smaller: newest
    uploads first (so new large scans don't wait), then the largest."""
    q = db.query(Document).filter(Document.storage_path.isnot(None))
    if only:
        q = q.filter(Document.id.in_(only))
    else:
        q = q.filter(Document.document_type.in_(list(types)))
    out = []
    for d in q:
        if (d.extracted_fields or {}).get("shrunk"):
            continue
        p = vault.abs_path(d.storage_path)
        if p is None or not p.exists() or p.suffix.lower() != ".pdf":
            continue
        size = p.stat().st_size
        if size >= min_mb * 1024 * 1024 or only:
            out.append((d, p, size))
    recent = _now().timestamp() - 2 * 86400
    out.sort(key=lambda x: (-(x[0].uploaded_at.timestamp() > recent if x[0].uploaded_at else 0), -x[2]))
    return out


def try_one(db: Session, d: Document, path: Path, apply: bool = False, timeout: int = 240,
            reader=None) -> dict:
    """Shrink one document if it is safe. Returns a report row."""
    if reader is None:
        from scripts.check_all_documents import read_isolated as reader
    csp = db.get(CSP, d.csp_id)
    data = path.read_bytes()
    row = {"document_id": d.id, "csp": csp.current_code if csp else "", "type": d.document_type,
           "file": path.name, "before_mb": round(len(data) / 1048576, 2), "after_mb": None, "saving_pct": None,
           "same_reading": None, "differs": "", "action": ""}
    scanned = is_scanned_pdf(data)
    if scanned is None:
        row["action"] = "skipped: not a readable PDF"
        return row
    if not scanned:
        row["action"] = "skipped: digital PDF (real text), already small"
        return row
    small = shrink_pdf(data)
    if small is None:
        row["action"] = f"skipped: would not be {int(MIN_SAVING * 100)}% smaller"
        return row
    row.update(after_mb=round(len(small) / 1048576, 2), saving_pct=round(100 * (1 - len(small) / len(data))))
    with tempfile.TemporaryDirectory(prefix="shrink_") as tmp:
        twin = Path(tmp) / path.name                  # the same name: the reader also looks at it
        twin.write_bytes(small)
        before = reader(path, d.document_type, timeout)
        after = reader(twin, d.document_type, timeout)
    diffs = same_reading(before, after)
    row["same_reading"] = not diffs
    row["differs"] = ", ".join(f"{k}: {before.get(k)} -> {after.get(k)}" for k in diffs if k != "could not read") \
        or ("could not read" if diffs else "")
    if diffs:
        row["action"] = "kept the original: the smaller copy reads differently"
        return row
    if not apply:
        row["action"] = "would shrink (dry run)"
        return row
    keep = vault.ROOT / vault.ORIGINALS / date.today().isoformat() / Path(d.storage_path)
    keep.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(small)
    shutil.copy2(path, keep)                          # the original is safe before anything is replaced
    part.replace(path)
    d.file_size_bytes = len(small)
    d.extracted_fields = {**(d.extracted_fields or {}), "shrunk": {
        "at": _now().isoformat(timespec="seconds"), "from_bytes": len(data), "to_bytes": len(small),
        "stored_sha256": hashlib.sha256(small).hexdigest(), "original": vault.rel(keep),
        "long_side": SHRINK_LONG_SIDE, "quality": SHRINK_QUALITY}}
    db.add(AgreementEvent(csp_id=d.csp_id, event_type="FILE_SHRUNK", source="shrink", channel="SYSTEM",
                          payload={"document_id": d.id, "from_mb": row["before_mb"], "to_mb": row["after_mb"],
                                   "original_kept": vault.rel(keep)}))
    db.commit()
    row["action"] = "shrunk"
    logger.info("shrunk doc=%s %.2f MB -> %.2f MB", d.id, row["before_mb"], row["after_mb"])
    return row
