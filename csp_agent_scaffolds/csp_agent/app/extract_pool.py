"""
app/extract_pool.py
Read several files at once (the attachments of one email). Extraction is
pure (no database), so it runs in threads; Tesseract itself runs as separate
processes, limited overall by OCR_WORKERS (app/ocr_service.py), so pages and
files share the CPU without overloading it. Results come back in the order
given; saving to the database stays with the caller, in that order.
"""
from concurrent.futures import ThreadPoolExecutor

from .ai.extraction.deterministic_extractor import extract_document_fields_deterministic
from .config import OCR_WORKERS


def extract_all(items: list[tuple[bytes, str]], allow_llm_fallback: bool = True) -> list[dict]:
    if len(items) <= 1:
        return [extract_document_fields_deterministic(b, n, allow_llm_fallback=allow_llm_fallback) for b, n in items]
    with ThreadPoolExecutor(max_workers=min(len(items), OCR_WORKERS)) as pool:
        return list(pool.map(lambda it: extract_document_fields_deterministic(
            it[0], it[1], allow_llm_fallback=allow_llm_fallback), items))
