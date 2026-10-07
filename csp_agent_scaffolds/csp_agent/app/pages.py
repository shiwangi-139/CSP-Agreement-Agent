"""
app/pages.py
Photos from the upload page -> one compact PDF per document.

A CSP photographs each page with the phone camera; the page (app/web/
portal.html) already shrinks every photo before sending. Here every photo
is shrunk again (in case the browser couldn't) and the pages are joined in
order into one PDF, so a 6-page agreement arrives as one small file and no
CSP needs a scanning app or to know what a PDF is.
"""
import io

MAX_SIDE_PX = 2000        # long side kept: plenty for OCR, a fraction of a phone photo's size
JPEG_QUALITY = 80
PDF_DPI = 150             # page size in the PDF (affects only how big it prints)
MAX_PAGES = 15


def shrink_photo(data: bytes) -> bytes:
    """JPEG, upright (EXIF rotation applied), long side at most MAX_SIDE_PX."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_SIDE_PX, MAX_SIDE_PX))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def photos_to_pdf(photos: list[bytes]) -> bytes:
    """One PDF page per photo, in the order given."""
    import fitz
    from PIL import Image
    doc = fitz.open()
    for jpg in photos:
        w, h = Image.open(io.BytesIO(jpg)).size
        page = doc.new_page(width=w * 72 / PDF_DPI, height=h * 72 / PDF_DPI)
        page.insert_image(page.rect, stream=jpg)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
