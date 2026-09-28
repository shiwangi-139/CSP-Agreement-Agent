"""
app/ocr_service.py
Robust Multi-Tier OCR & Allowlist Pre-Filtering Engine.
- Strictly allows ONLY 3 document types:
  1. CSP Agreement
  2. Police Verification / Character Certificate (English & Hindi)
  3. IIBF Certificate
- Immediately rejects unauthorized documents (Aadhaar, PAN, Bank Statements, Photos).
- Supports digital text extraction via PyMuPDF with seamless OCR fallback.
- Provides interactive side-by-side OCR engine benchmarking.
"""

import io
import os
import re
import time
import logging
from typing import Tuple, Dict, Any, Optional
import fitz  # PyMuPDF
from PIL import Image

# Ensure custom Hindi tessdata model is recognized if present
TESSDATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "tessdata"))
if os.path.exists(TESSDATA_DIR) and "TESSDATA_PREFIX" not in os.environ:
    os.environ["TESSDATA_PREFIX"] = TESSDATA_DIR

logger = logging.getLogger(__name__)


# Permitted document keywords
AGREEMENT_SIGNATURES = [
    "customer service point agreement", "csp agreement", "service point agreement",
    "india non judicial", "non-judicial stamp", "agreement for customer service point",
    "kiosk banking agreement", "business correspondent agreement", "stamp duty",
    "memorandum of an agreement", "appointment of csp of eko", "eko india financial services",
    "eko bharat vantures", "article 5 agreement"
]

PVR_SIGNATURES = [
    "police verification", "character certificate", "character & antecedent",
    "police clearance", "pvr", "pcc", "cctns", "superintendent of police",
    "चरित्र प्रमाण पत्र", "पुलिस सत्यापन", "पुलिस अधीक्षक", "निर्गत तिथि"
]

IIBF_SIGNATURES = [
    "indian institute of banking and finance", "indian institute of banking & finance",
    "iibf", "bc/bf", "bcbf", "business correspondents", "bc / bf examination",
    "certificate examination for business correspondents"
]

# Strict Disallowlist Signatures (Zero Storage for standalone unauthorized docs)
UNAUTHORIZED_SIGNATURES = [
    "unique identification authority of india", "मेरा आधार",
    "income tax department", "permanent account number card",
    "account statement", "bank statement", "passbook", "statement of account",
    "curriculum vitae", "resume", "offer letter", "appointment letter", "salary slip"
]


def extract_pdf_pages_text(pdf_bytes: bytes, max_pages: int = 5) -> str:
    """Extracts text using PyMuPDF embedded text layer."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        for idx in range(min(len(doc), max_pages)):
            text += doc[idx].get_text() + "\n"
        return text.strip()
    except Exception as e:
        logger.warning(f"PyMuPDF text extraction failed: {e}")
        return ""


def run_tesseract_ocr(pdf_bytes: bytes, lang: str = "eng+hin", max_pages: int = 3, dpi: int = 200) -> Tuple[str, float]:
    """Runs image rendering and Tesseract OCR on scanned pages."""
    start_time = time.time()
    ocr_text = ""
    try:
        import pytesseract
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        # Check available tesseract languages
        try:
            installed_langs = pytesseract.get_languages()
            use_lang = lang if ("hin" in installed_langs or "+" not in lang) else "eng"
        except Exception:
            use_lang = "eng"

        for page_idx in range(min(len(doc), max_pages)):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            # Image preprocessing: Convert to grayscale for cleaner contrast
            img_gray = img.convert("L")
            page_text = pytesseract.image_to_string(img_gray, lang=use_lang)
            ocr_text += f"\n--- Page {page_idx + 1} ---\n" + page_text

        duration = time.time() - start_time
        return ocr_text.strip(), duration
    except Exception as e:
        logger.warning(f"Tesseract OCR failed: {e}")
        return "", time.time() - start_time


_easyocr_reader = None


def get_easyocr_reader():
    """Lazily initializes and caches EasyOCR reader, storing models on the large DATA disk if available."""
    global _easyocr_reader
    if _easyocr_reader is None:
        try:
            import easyocr
            model_dir = os.getenv("EASYOCR_MODEL_DIR")
            if not model_dir and os.path.exists("/run/media/shiwangis-sinha/DATA"):
                model_dir = "/run/media/shiwangis-sinha/DATA/easyocr_models"
            if model_dir:
                os.makedirs(model_dir, exist_ok=True)
                _easyocr_reader = easyocr.Reader(['en', 'hi'], model_storage_directory=model_dir, user_network_directory=model_dir, gpu=False)
            else:
                _easyocr_reader = easyocr.Reader(['en', 'hi'], gpu=False)
        except Exception as e:
            logger.debug(f"EasyOCR not available: {e}")
            return None
    return _easyocr_reader


def run_easyocr(pdf_bytes: bytes, max_pages: int = 3, dpi: int = 150) -> Tuple[str, float]:
    """Runs EasyOCR text extraction on rendered PDF pages."""
    start_time = time.time()
    reader = get_easyocr_reader()
    if not reader:
        return "", time.time() - start_time

    ocr_text = ""
    try:
        import numpy as np
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page_idx in range(min(len(doc), max_pages)):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            img_np = np.array(img)
            results = reader.readtext(img_np, detail=0)
            page_text = "\n".join(results)
            ocr_text += f"\n--- Page {page_idx + 1} ---\n" + page_text
        return ocr_text.strip(), time.time() - start_time
    except Exception as e:
        logger.warning(f"EasyOCR execution failed: {e}")
        return "", time.time() - start_time


def extract_document_text(pdf_bytes: bytes) -> Tuple[str, str, float]:
    """
    Tiered extraction:
    1. PyMuPDF Digital text layer.
    2. If text < 100 characters:
       - Uses EasyOCR if OCR_ENGINE=easyocr and package is installed.
       - Otherwise uses bilingual Tesseract OCR (eng+hin).
    Returns (extracted_text, method_used, duration_seconds).
    """
    start = time.time()
    digital_text = extract_pdf_pages_text(pdf_bytes)
    if len(digital_text) >= 100:
        return digital_text, "DIGITAL_PYMUPDF", time.time() - start

    ocr_engine = os.getenv("OCR_ENGINE", "tesseract").lower()
    if ocr_engine == "easyocr":
        easy_text, easy_time = run_easyocr(pdf_bytes)
        if len(easy_text) > 0:
            return easy_text, "EASYOCR", easy_time

    # Fallback to local OCR for scans
    ocr_text, ocr_time = run_tesseract_ocr(pdf_bytes)
    if len(ocr_text) > 0:
        return ocr_text, "BILINGUAL_TESSERACT_OCR", ocr_time

    # Secondary fallback to EasyOCR if installed
    if ocr_engine != "easyocr":
        easy_text, easy_time = run_easyocr(pdf_bytes)
        if len(easy_text) > 0:
            return easy_text, "EASYOCR_FALLBACK", easy_time

    return digital_text, "EMPTY_FALLBACK", time.time() - start


# Phrases that identify the document by themselves. Company names, "stamp
# duty" etc. appear on many other Eko papers (e.g. the Business Continuity
# Plan) and are NOT enough on their own.
STRONG_AGREEMENT = [
    r"customer\s*service\s*point\s*agreement", r"\bcsp\s+agreement", r"service\s+point\s+agreement",
    r"kiosk\s+banking\s+agreement", r"business\s+correspondent\s+agreement",
    r"agreement\s+for\s+customer\s+service\s+point", r"article\s*[5§]\s*agreement",
    r"memorandum\s+of\s+an?\s+agreement", r"appointment\s+of\s+csp\s+of\s+eko",
]
STRONG_PVR = [
    r"police\s+verification", r"character\s+certificate", r"character\s*(?:&|and)\s*antecedent",
    r"police\s+clearance", r"\bpvr\b", r"\bpcc\b", r"\bcctns\b",
    r"चरित्र\s*प्रमाण\s*पत्र", r"पुलिस\s*सत्यापन", r"चरित्र\s*सत्यापन",
]
STRONG_IIBF = [
    r"indian\s+institute\s+of\s+banking", r"institute\s+of\s+banking\s*(?:&|and)\s*finance",
    r"\biibf\b", r"\bbc\s*/\s*bf\b", r"\bbcbf\b", r"certificate\s+examination\s+for\s+business\s+correspondents",
]


def _matches(patterns, text):
    return [p for p in patterns if re.search(p, text)]


def classify_allowlist_gate(text: str, filename: str = "") -> Tuple[bool, str, str, float]:
    """
    Is this one of the three permitted documents?
    Returns (is_allowed, document_type, reason, confidence).
    Permitted types are checked first so an agreement that mentions PAN or
    Aadhaar in its clauses is not rejected.
    """
    combined = f"{filename} {text}".lower()
    body = (text or "").lower()

    for doc_type, patterns in (("AGREEMENT", STRONG_AGREEMENT), ("POLICE_VERIFICATION", STRONG_PVR),
                               ("IIBF_CERTIFICATE", STRONG_IIBF)):
        hits = _matches(patterns, body)
        if hits:
            conf = 0.95 if len(hits) >= 2 else 0.9
            return True, doc_type, f"Matched {doc_type} phrases: {hits[:3]}", conf

    # Filename-only hints are weak but useful for photos OCR couldn't read.
    fn = (filename or "").lower()
    for doc_type, words in (("AGREEMENT", ("agreement", "agrmnt", "csp_agr")),
                            ("POLICE_VERIFICATION", ("pvr", "police", "character", "charitra")),
                            ("IIBF_CERTIFICATE", ("iibf", "bcbf", "bc_bf"))):
        if any(w in fn for w in words) and len(body.strip()) < 200:
            return True, doc_type, f"Filename suggests {doc_type}; text too short to confirm.", 0.6

    for unauth in UNAUTHORIZED_SIGNATURES:
        if unauth in combined:
            return False, "REJECTED_UNAUTHORIZED", f"Contains unauthorized document pattern: '{unauth}'", 0.99
    if any(k in fn for k in ["pan", "aadhaar", "adhar", "passbook", "statement", "resume"]):
        return False, "REJECTED_UNAUTHORIZED", f"Filename indicates unauthorized document: '{filename}'", 0.99

    return False, "UNKNOWN", "Document did not match any permitted document types (Agreement, PVR, IIBF)", 0.0


def check_explicit_3year_clause(text: str) -> bool:
    """
    Deterministically determines if an agreement explicitly specifies 3-year validity.
    Examples:
    - "This agreement shall remain valid for 3 years"
    - "valid for a period of three (3) years"
    - "period of 3 years from the date"
    """
    text_clean = re.sub(r'\s+', ' ', text.lower())
    patterns = [
        r'valid\s+for\s+(?:a\s+period\s+of\s+)?(?:three|3)\s*(?:\([0-9]\)\s*)?years?',
        r'period\s+of\s+(?:three|3)\s*(?:\([0-9]\)\s*)?years?',
        r'validity\s*(?:is|shall\s+be|of)\s*(?:three|3)\s*(?:\([0-9]\)\s*)?years?',
        r'shall\s+remain\s+(?:in\s+force|valid)\s+for\s+(?:three|3)\s*years?',
        r'duration\s+of\s+(?:three|3)\s*years?'
    ]
    return any(re.search(pat, text_clean) for pat in patterns)


def benchmark_ocr_engines(pdf_bytes: bytes) -> Dict[str, Any]:
    """Runs a side-by-side comparison on an uploaded PDF for diagnostic evaluation."""
    results = {}

    # Engine 1: PyMuPDF Text Layer
    t0 = time.time()
    pymupdf_text = extract_pdf_pages_text(pdf_bytes)
    t1 = time.time()
    results["PyMuPDF"] = {
        "text_snippet": pymupdf_text[:300] if pymupdf_text else "[No selectable text]",
        "chars_extracted": len(pymupdf_text),
        "duration_ms": round((t1 - t0) * 1000, 2),
        "is_scanned": len(pymupdf_text) < 100
    }

    # Engine 2: Tesseract OCR (eng)
    t2 = time.time()
    tess_eng_text, _ = run_tesseract_ocr(pdf_bytes, lang="eng", max_pages=2)
    t3 = time.time()
    results["Tesseract_ENG"] = {
        "text_snippet": tess_eng_text[:300] if tess_eng_text else "[OCR produced no text]",
        "chars_extracted": len(tess_eng_text),
        "duration_ms": round((t3 - t2) * 1000, 2)
    }

    # Engine 3: Tesseract OCR (eng+hin)
    t4 = time.time()
    tess_hin_text, _ = run_tesseract_ocr(pdf_bytes, lang="eng+hin", max_pages=2)
    t5 = time.time()
    results["Tesseract_Bilingual"] = {
        "text_snippet": tess_hin_text[:300] if tess_hin_text else "[OCR produced no text]",
        "chars_extracted": len(tess_hin_text),
        "duration_ms": round((t5 - t4) * 1000, 2)
    }

    return results



# =============================================================================
# Page-level scanning: every page, photos too, with real blur and OCR
# confidence measurements. This is what the extractor uses; the helpers above
# are kept for the benchmark endpoint and older scripts.
# =============================================================================
from dataclasses import dataclass, field
from typing import Callable, List

from .config import OCR_MAX_PAGES, OCR_DPI, BLUR_THRESHOLD, MIN_IMAGE_SIDE_PX

MIN_PAGE_TEXT_LAYER = 200   # below this, a PDF page is treated as scanned
# A text layer shorter than this may be just a stamp or signature over an
# image (IIBF certificates), so the page image is OCR'd as well.
SHORT_TEXT_LAYER = 700
MIN_READABLE_CHARS = 60
MIN_OCR_CONFIDENCE = 45.0   # mean Tesseract word confidence, 0-100


@dataclass
class PageScan:
    index: int
    text: str
    source: str                   # DIGITAL or OCR
    ocr_confidence: Optional[float] = None
    blur_score: Optional[float] = None
    width: int = 0
    height: int = 0
    image_png: Optional[bytes] = field(default=None, repr=False)

    @property
    def is_blurry(self) -> bool:
        return self.source == "OCR" and self.blur_score is not None and self.blur_score < BLUR_THRESHOLD

    @property
    def is_readable(self) -> bool:
        if len(self.text.strip()) < MIN_READABLE_CHARS:
            return False
        if self.source == "DIGITAL":
            return True
        return (self.ocr_confidence or 0) >= MIN_OCR_CONFIDENCE and not self.is_blurry


@dataclass
class DocumentScan:
    mime_type: Optional[str]
    pages: List[PageScan]
    page_count: int
    source_bytes: Optional[bytes] = field(default=None, repr=False)

    def page_image(self, index: int) -> Optional[bytes]:
        """The page as PNG/JPEG for the vision model (rendered only now)."""
        for p in self.pages:
            if p.index == index and p.image_png:
                return p.image_png
        return page_png(self.source_bytes, index) if self.source_bytes else None

    @property
    def text(self) -> str:
        return "\n".join(f"\n--- Page {p.index + 1} ---\n{p.text}" for p in self.pages).strip()

    @property
    def method(self) -> str:
        sources = {p.source for p in self.pages}
        if sources == {"DIGITAL"}:
            return "DIGITAL_PYMUPDF"
        if sources == {"OCR"}:
            return "BILINGUAL_TESSERACT_OCR"
        return "HYBRID_TEXT_LAYER_PLUS_OCR" if sources else "EMPTY"

    @property
    def readable_pages(self) -> int:
        return sum(1 for p in self.pages if p.is_readable)

    def readability(self) -> tuple[bool, str]:
        if not self.pages:
            return False, "No pages could be opened."
        if self.readable_pages == 0:
            if any(p.is_blurry for p in self.pages):
                return False, "Image is too blurry to read."
            return False, "No readable text found on any page."
        return True, f"{self.readable_pages}/{len(self.pages)} pages readable."


def detect_mime(data: bytes) -> Optional[str]:
    """File type from the bytes themselves, never from the filename or the
    client's Content-Type header."""
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def _blur_score(gray) -> float:
    import cv2
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _prepare_for_ocr(gray):
    """Upscale small images, boost contrast and straighten slight skew."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    if max(h, w) < 1600:
        scale = 1600 / max(h, w)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

    try:
        inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        coords = np.column_stack(np.where(inv > 0))
        if len(coords) > 500:
            angle = cv2.minAreaRect(coords.astype(np.float32))[-1]
            angle = angle - 90 if angle > 45 else angle
            if 0.5 < abs(angle) < 10:
                hh, ww = gray.shape[:2]
                m = cv2.getRotationMatrix2D((ww / 2, hh / 2), angle, 1.0)
                gray = cv2.warpAffine(gray, m, (ww, hh), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    except Exception as e:  # deskew is best-effort
        logger.debug(f"deskew skipped: {e}")
    return gray


# ---------------------------------------------------------------- speed
# Tesseract runs as its own process, so plain threads give real parallelism.
# One shared limit covers pages AND attachments, so the CPU is never
# oversubscribed however the work is split.
import hashlib
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import OCR_WORKERS, OCR_CACHE

_TESS_SLOTS = threading.BoundedSemaphore(OCR_WORKERS)
if OCR_WORKERS > 1:
    # Several Tesseract runs share the CPU; one thread each is fastest.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
# Seconds spent per stage in this process (read by scripts/benchmark_pipeline.py).
TIMINGS: Counter = Counter()
_timings_lock = threading.Lock()
# Pages 1-3 are read one at a time (the fields are usually there); the rest
# in parallel batches, stopping after the first batch that completes them.
SEQUENTIAL_PAGES = 3
# An English page read this well needs no Hindi pass.
ENGLISH_CONFIDENT = 75.0
ENGLISH_MAX_LOW_WORDS = 0.10
CACHE_VERSION = "ocr-v2"
_cache_on = OCR_CACHE


def _timed(stage: str, t0: float) -> None:
    with _timings_lock:
        TIMINGS[stage] += time.perf_counter() - t0


def set_cache_enabled(on: bool) -> None:
    global _cache_on
    _cache_on = bool(on)


def _ocr_one(gray, lang: str) -> tuple[str, float, float]:
    """Text, mean word confidence, and the share of words read with low
    confidence (high when the page is in another script)."""
    import pytesseract

    t0 = time.perf_counter()
    with _TESS_SLOTS:
        data = pytesseract.image_to_data(gray, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT)
    _timed(f"ocr_{lang}", t0)
    confs = []
    lines: dict = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        confs.append(conf)
    text = "\n".join(" ".join(ws) for ws in lines.values())
    low = sum(1 for c in confs if c < 40) / len(confs) if confs else 1.0
    return text, (sum(confs) / len(confs) if confs else 0.0), low


_LANGS: Optional[list] = None


def _languages() -> list:
    global _LANGS
    if _LANGS is None:
        import pytesseract
        try:
            _LANGS = list(pytesseract.get_languages())
        except Exception:
            _LANGS = []
    return _LANGS


def english_is_enough(text: str, conf: float, low_share: float) -> bool:
    """A clean English page: confident words and hardly any garbage (Hindi
    read as English comes out as many low-confidence fragments)."""
    return (conf >= ENGLISH_CONFIDENT and low_share < ENGLISH_MAX_LOW_WORDS
            and len(re.findall(r"[A-Za-z]{3,}", text)) >= 20)


def _ocr_image(gray) -> tuple[str, float]:
    """English pass, then a Hindi pass only if the page isn't clearly
    English. Tesseract's combined "eng+hin" mode is ~15x slower than running
    the two models one after the other, so we run them separately and keep
    the Hindi text only when it is genuinely readable (a Hindi certificate),
    so garbage from reading an English page as Hindi can't inject false dates."""
    eng_text, eng_conf, eng_low = _ocr_one(gray, "eng")
    if "hin" not in _languages() or english_is_enough(eng_text, eng_conf, eng_low):
        return eng_text, eng_conf
    hin_text, hin_conf, _ = _ocr_one(gray, "hin")
    if hin_conf >= 60 or hin_conf > eng_conf - 10:
        return f"{eng_text}\n{hin_text}", max(eng_conf, hin_conf)
    return eng_text, eng_conf


def _gray_from_png(png: bytes):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)


def _render_gray(pdf_page, dpi: int):
    import numpy as np
    t0 = time.perf_counter()
    pix = pdf_page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].copy()
    _timed("render", t0)
    return gray


def _ocr_page(gray, layer: str) -> tuple[str, float, float, str]:
    """OCR one rendered page: (text, confidence, blur, source)."""
    t0 = time.perf_counter()
    blur = _blur_score(gray)
    prepared = _prepare_for_ocr(gray)
    _timed("prepare", t0)
    text, conf = _ocr_image(prepared)
    # Keep any small text layer too (e.g. the IIBF digital signature).
    if layer.strip():
        text = f"{text}\n{layer}"
    source = "OCR"
    if len(layer.strip()) >= MIN_PAGE_TEXT_LAYER:
        source, conf = "DIGITAL", max(conf, 90.0)  # the text layer alone is trustworthy
    return text, conf, blur, source


# ---------------------------------------------------------------- cache
def _cache_file(sha256: str) -> Path:
    from . import vault
    return vault.ROOT.parent / "ocr_cache" / sha256[:2] / f"{sha256}.json"


def _cache_key() -> str:
    return f"{CACHE_VERSION}:dpi{OCR_DPI}"


def _cache_load(sha256: str) -> dict:
    if not _cache_on:
        return {}
    try:
        data = json.loads(_cache_file(sha256).read_text())
        return data.get("pages", {}) if data.get("key") == _cache_key() else {}
    except (OSError, ValueError):
        return {}


def _cache_save(sha256: str, pages: List["PageScan"]) -> None:
    if not _cache_on or not pages:
        return
    try:
        f = _cache_file(sha256)
        f.parent.mkdir(parents=True, exist_ok=True)
        old = _cache_load(sha256)
        old.update({str(p.index): {"text": p.text, "source": p.source, "conf": p.ocr_confidence,
                                   "blur": p.blur_score, "w": p.width, "h": p.height} for p in pages})
        tmp = f.with_suffix(".part")
        tmp.write_text(json.dumps({"key": _cache_key(), "pages": old}))
        tmp.replace(f)
    except OSError as e:
        logger.debug(f"ocr cache not written: {e}")


def _from_cache(idx: int, c: dict) -> "PageScan":
    return PageScan(idx, c["text"], c["source"], c.get("conf"), c.get("blur"), c.get("w", 0), c.get("h", 0))


# ---------------------------------------------------------------- scanning
def page_png(file_bytes: bytes, index: int, dpi: int = 150) -> Optional[bytes]:
    """One page as PNG, rendered only when the vision model needs it."""
    if detect_mime(file_bytes) != "application/pdf":
        return file_bytes
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            return doc[index].get_pixmap(dpi=dpi).tobytes("png")
    except Exception as e:
        logger.warning(f"page render failed: {e}")
        return None


def scan_document(file_bytes: bytes,
                  stop_when: Optional[Callable[[str], bool]] = None,
                  max_pages: int = OCR_MAX_PAGES) -> DocumentScan:
    """Read every page (up to max_pages) of a PDF or photo.

    PDF pages with a real text layer use it (and are never rendered); pages
    without one (scans, or IIBF certificates whose body is an image) are
    rendered in grayscale and OCR'd. `stop_when(text_so_far)` lets the caller
    stop early once the fields it needs are found. OCR results are cached by
    the file's SHA-256, so the same file is never OCR'd twice.
    """
    mime = detect_mime(file_bytes)
    if mime is None:
        return DocumentScan(None, [], 0)
    sha = hashlib.sha256(file_bytes).hexdigest()
    cached = _cache_load(sha)
    new_pages: List[PageScan] = []

    if mime.startswith("image/"):
        if "0" in cached:
            page = _from_cache(0, cached["0"])
        else:
            gray = _gray_from_png(file_bytes)  # imdecode reads JPEG too
            if gray is None:
                return DocumentScan(mime, [], 0, file_bytes)
            h, w = gray.shape[:2]
            t0 = time.perf_counter()
            blur = _blur_score(gray)
            prepared = _prepare_for_ocr(gray)
            _timed("prepare", t0)
            text, conf = _ocr_image(prepared)
            page = PageScan(0, text, "OCR", conf, blur, w, h)
            if max(h, w) < MIN_IMAGE_SIDE_PX:
                page.ocr_confidence = min(conf, MIN_OCR_CONFIDENCE - 1)  # too small to trust
            new_pages.append(page)
        page.image_png = file_bytes
        _cache_save(sha, new_pages)
        return DocumentScan(mime, [page], 1, file_bytes)

    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
    except Exception as e:
        logger.warning(f"PDF could not be opened: {e}")
        return DocumentScan(mime, [], 0, file_bytes)

    page_count = len(doc)
    last = min(page_count, max_pages)
    pages: List[PageScan] = []

    def prepare(idx: int):
        """Main thread (PyMuPDF isn't thread-safe): a finished PageScan, or
        the rendered image still to OCR."""
        if str(idx) in cached:
            return _from_cache(idx, cached[str(idx)]), None, None
        pdf_page = doc[idx]
        layer = pdf_page.get_text() or ""
        has_images = bool(pdf_page.get_images(full=False))
        w, h = int(pdf_page.rect.width * OCR_DPI / 72), int(pdf_page.rect.height * OCR_DPI / 72)
        if len(layer.strip()) >= SHORT_TEXT_LAYER or (len(layer.strip()) >= MIN_PAGE_TEXT_LAYER and not has_images):
            ps = PageScan(idx, layer, "DIGITAL", None, None, w, h)
            new_pages.append(ps)
            return ps, None, None
        return None, _render_gray(pdf_page, OCR_DPI), layer

    def finish(idx: int, gray, layer: str) -> PageScan:
        text, conf, blur, source = _ocr_page(gray, layer)
        ps = PageScan(idx, text, source, conf, blur, gray.shape[1], gray.shape[0])
        new_pages.append(ps)
        return ps

    def stop() -> bool:
        if stop_when is None:
            return False
        try:
            return bool(stop_when("\n".join(p.text for p in pages)))
        except Exception:
            return False

    try:
        idx = 0
        while idx < min(last, SEQUENTIAL_PAGES):
            ps, gray, layer = prepare(idx)
            pages.append(ps if ps is not None else finish(idx, gray, layer))
            idx += 1
            if stop():
                return DocumentScan(mime, pages, page_count, file_bytes)
        with ThreadPoolExecutor(max_workers=OCR_WORKERS) as pool:
            while idx < last:
                batch = list(range(idx, min(last, idx + OCR_WORKERS)))
                prepared = [(i, *prepare(i)) for i in batch]
                futures = {i: pool.submit(finish, i, gray, layer) for i, ps, gray, layer in prepared if ps is None}
                for i, ps, _, _ in prepared:
                    pages.append(ps if ps is not None else futures[i].result())
                idx = batch[-1] + 1
                if stop():
                    break
    finally:
        doc.close()
        _cache_save(sha, new_pages)
    return DocumentScan(mime, pages, page_count, file_bytes)
