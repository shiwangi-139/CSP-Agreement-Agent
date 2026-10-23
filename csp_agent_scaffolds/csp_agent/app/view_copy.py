"""
app/view_copy.py
A light copy of a big stored document, for viewing on the dashboard: an
agreement scanned at high quality is often 20-27 MB and took minutes to open
over the server's line. The view copy has each page as a screen-quality JPEG
(VIEW_DPI, VIEW_QUALITY) in the same page size, usually 1-2 MB. The original
is never changed; "download original" still serves it.

Copies live in <vault parent>/view_copies/<document id>.<ext>, are made the
first time a document is viewed (or by scripts/make_view_copies.py at night),
and are made again if the original changes. Files under SMALL_ENOUGH are
served as they are.
"""
import logging
from pathlib import Path
from typing import Optional

from . import vault

logger = logging.getLogger(__name__)

SMALL_ENOUGH = 2 * 1024 * 1024      # bytes: no copy needed below this
VIEW_DPI = 130
VIEW_QUALITY = 65
MAX_IMAGE_SIDE = 1800
DIR = vault.ROOT.parent / "view_copies"


def _target(doc_id: int, src: Path) -> Path:
    return DIR / f"{doc_id}{'.pdf' if src.suffix.lower() == '.pdf' else '.jpg'}"


def _make_pdf(src: Path, dst: Path) -> None:
    import pymupdf
    with pymupdf.open(src) as doc, pymupdf.open() as out:
        for page in doc:
            pix = page.get_pixmap(dpi=VIEW_DPI)
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=pix.tobytes("jpg", jpg_quality=VIEW_QUALITY))
        out.save(dst, garbage=3, deflate=True)


def _make_image(src: Path, dst: Path) -> None:
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        im.save(dst, "JPEG", quality=VIEW_QUALITY + 10, optimize=True)


def view_file(doc_id: int, src: Path) -> tuple[Path, Optional[str]]:
    """(file to show, its media type if it is a copy). The original when it is
    small, or when no copy can be made."""
    if src.stat().st_size <= SMALL_ENOUGH:
        return src, None
    dst = _target(doc_id, src)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst, "application/pdf" if dst.suffix == ".pdf" else "image/jpeg"
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        (_make_pdf if src.suffix.lower() == ".pdf" else _make_image)(src, tmp)
        if tmp.stat().st_size >= src.stat().st_size:      # not smaller: show the original
            tmp.unlink()
            return src, None
        tmp.replace(dst)
        logger.info("view_copy doc=%s %.1f MB -> %.1f MB", doc_id, src.stat().st_size / 1e6, dst.stat().st_size / 1e6)
        return dst, "application/pdf" if dst.suffix == ".pdf" else "image/jpeg"
    except Exception:
        logger.exception("view_copy_failed doc=%s", doc_id)
        return src, None
