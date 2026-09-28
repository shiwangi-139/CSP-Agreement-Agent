"""
app/storage.py
Small file helpers. Where documents live and what they are called is decided
by app/vault.py (one folder per CSP, names like 1A850004_PVR_ACTIVE_...pdf).
"""

import logging
import re
from typing import Optional

from .vault import abs_path, csp_folder  # noqa: F401  (re-exported for older imports)

logger = logging.getLogger(__name__)


def sanitize_filename_part(name: str) -> str:
    """Replaces unsafe characters with underscores, keeping alphanumerics and hyphens."""
    if not name:
        return "doc"
    clean = re.sub(r'[^a-zA-Z0-9_\-]', '_', name.strip())
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean[:30] or "doc"


def delete_file(storage_path: str) -> bool:
    """Safely removes an unauthorized or purged document from disk."""
    if not storage_path:
        return False
    try:
        p = abs_path(storage_path)
        if p is not None and p.exists() and p.is_file():
            p.unlink()
            return True
    except Exception as e:
        logger.warning(f"Failed to delete file {storage_path}: {e}")
    return False


def read_file(storage_path: str) -> bytes:
    """Reads binary document bytes from storage path."""
    p = abs_path(storage_path)
    if p is None or not p.exists():
        return b""
    return p.read_bytes()


def get_human_readable_download_name(
    csp_code: str,
    csp_name: str,
    doc_type: str,
    year: Optional[int | str] = None,
    ext: str = "pdf"
) -> str:
    """
    Generates a clean, professional download filename for browser Content-Disposition.
    Example: '1A852474_Sohit_Kumar_CSP_Agreement_2025.pdf'
    """
    code_part = sanitize_filename_part(csp_code or "CSP").upper()
    name_part = sanitize_filename_part(csp_name or "Partner").title()

    type_map = {
        "AGREEMENT": "CSP_Agreement",
        "POLICE_VERIFICATION": "Police_Verification",
        "CHARACTER_CERTIFICATE": "Character_Certificate",
        "IIBF_CERTIFICATE": "IIBF_Certificate",
    }
    type_part = type_map.get((doc_type or "").upper(), sanitize_filename_part(doc_type or "Document"))
    year_part = f"_{year}" if year else ""
    clean_ext = ext.lstrip(".")

    return f"{code_part}_{name_part}_{type_part}{year_part}.{clean_ext}"
