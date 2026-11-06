"""
The file name as a hint for the document type. Emails often name each file
("1A850853 Agreement MATHURA PRASAD.pdf", "PVR 1A851336.pdf", "IIBF.pdf");
on 06-11-2026 17 documents were saved as another type than their file name
says, 5 of them counted as that other type (an agreement counted as a PVR).

A file whose name says exactly one type but reads as another is read again
as the named type; if it still reads as another, a person checks the type
(it doesn't count meanwhile). Used by Gmail (app/gmail_ingest.py) and
scripts/fix_type_from_filename.py.
"""
import re
from typing import Callable, Optional

from .compliance import DOC_LABELS, canonical_type

NAMED = {
    "AGREEMENT": re.compile(r"AGREE?MENT"),
    "POLICE_VERIFICATION": re.compile(r"(?<![A-Z])(?:PVR|PCC)(?![A-Z])|POLICE|CHARACTER"),
    "IIBF_CERTIFICATE": re.compile(r"(?<![A-Z])IIBF(?![A-Z])"),
}


def type_named_in(filename: Optional[str]) -> Optional[str]:
    """The one document type a file name names, or None (none, or several)."""
    name = re.sub(r"[_\-.]+", " ", (filename or "").upper())
    says = [t for t, p in NAMED.items() if p.search(name)]
    return says[0] if len(says) == 1 else None


def label(t: Optional[str]) -> str:
    return DOC_LABELS.get(canonical_type(t), (str(t or "unknown").replace("_", " ").lower(),))[0]


def recheck_with_name(data: bytes, filename: str, ex: dict, read: Optional[Callable] = None) -> dict:
    """ex, or a better reading when the file name says another type."""
    named = type_named_in(filename)
    got = canonical_type(ex.get("document_type"))
    if not named or ex.get("readability") != "READABLE" or got == named:
        return ex
    if read is None:
        from .ai.extraction.deterministic_extractor import extract_document_fields_deterministic as read
    again = read(data, filename, expected_type=named, allow_llm_fallback=False)
    if again.get("readability") == "READABLE" and canonical_type(again.get("document_type")) == named \
            and not again.get("type_review"):
        again["type_hint"] = f"Read as {label(got)} at first; the file name says {label(named)} and it reads as one."
        return again
    note = f"The file name ({filename}) says {label(named)}, but it reads as {label(got)}: check the type."
    return {**ex, "type_review": " ".join(x for x in (ex.get("type_review"), note) if x)}
