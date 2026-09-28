"""Deterministic, auditable extraction rules for the three CSP documents.

Each extractor returns plain fields plus the id of the rule that produced
them (date_source / validity_source), which is stored on the Document so
every date on the dashboard can be traced to the rule that found it.
"""
from .agreement import extract_agreement, has_three_year_clause
from .pvr import extract_pvr
from .iibf import extract_iibf
from .dates import find_dates, add_months, normalize

__all__ = ["extract_agreement", "has_three_year_clause", "extract_pvr", "extract_iibf",
           "find_dates", "add_months", "normalize"]
