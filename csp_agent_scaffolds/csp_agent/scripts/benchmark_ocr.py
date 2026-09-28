"""
scripts/benchmark_ocr.py
Side-by-side OCR engine diagnostic comparison.
Compares PyMuPDF (digital), Tesseract (Bilingual), and EasyOCR on any PDF.
Usage:
  python scripts/benchmark_ocr.py path/to/document.pdf
"""

import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.ocr_service import extract_pdf_pages_text, run_tesseract_ocr, run_easyocr


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/benchmark_ocr.py <path_to_pdf>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    if not os.path.exists(pdf_path):
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    print("\n" + "=" * 65)
    print(f"   OCR BENCHMARK DIAGNOSTIC: {os.path.basename(pdf_path)}")
    print("=" * 65)

    # 1. PyMuPDF Digital
    t0 = time.time()
    pymupdf_text = extract_pdf_pages_text(pdf_bytes, max_pages=3)
    t_pymupdf = (time.time() - t0) * 1000
    print(f"\n[1] PyMuPDF (Digital Text Layer)")
    print(f"    Speed: {t_pymupdf:.1f}ms | Characters: {len(pymupdf_text)}")
    print(f"    Sample: {pymupdf_text[:200].strip() or '[No embedded digital text - purely scanned]'}")

    # 2. Tesseract Bilingual (eng+hin)
    tess_text, t_tess = run_tesseract_ocr(pdf_bytes, lang="eng+hin", max_pages=3)
    print(f"\n[2] Tesseract OCR (Bilingual English + Hindi)")
    print(f"    Speed: {t_tess * 1000:.1f}ms | Characters: {len(tess_text)}")
    print(f"    Sample: {tess_text[:200].strip() or '[OCR produced 0 characters]'}")

    # 3. EasyOCR
    easy_text, t_easy = run_easyocr(pdf_bytes, max_pages=3)
    if easy_text or t_easy > 0.05:
        print(f"\n[3] EasyOCR (PyTorch CRAFT + CRNN)")
        print(f"    Speed: {t_easy * 1000:.1f}ms | Characters: {len(easy_text)}")
        print(f"    Sample: {easy_text[:200].strip() or '[EasyOCR produced 0 characters]'}")
    else:
        print(f"\n[3] EasyOCR: Not installed (run 'pip install easyocr' to test)")

    print("\n" + "=" * 65 + "\n")


if __name__ == "__main__":
    main()
