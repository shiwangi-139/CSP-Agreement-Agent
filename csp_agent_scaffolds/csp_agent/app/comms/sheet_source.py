"""
Read-only access to the calling sheet, limited to ONE tab.

CALLING_SHEET_SOURCE=local_xlsx (default, testing): reads only the
CALLING_SHEET_TAB tab of CALLING_SHEET_LOCAL_PATH with openpyxl in
read-only mode. Nothing is downloaded and nothing is written.

CALLING_SHEET_SOURCE=google (deployment): reads only that tab through the
Sheets API with the spreadsheets.readonly scope.

CALLING_SHEET_LINK (preferred when set): the live sheet's link, opened on
the "Calling Sheet New" tab. Only that tab (its gid) is read: as CSV if the
link is shared for viewing, otherwise through the service account. If the
live sheet can't be read, the saved copy is used and the reason logged.

Either way the result is the same: a list of dicts keyed by the header text
of the tab's header row, so the sync logic doesn't care where rows came from.
"""
import csv
import io
import json
import logging
import os
import re
import urllib.request
from typing import Any

from ..config import (
    CALLING_SHEET_SOURCE, CALLING_SHEET_LOCAL_PATH, CALLING_SHEET_TAB,
    CALLING_SHEET_SPREADSHEET_ID, CALLING_SHEET_LINK,
)

logger = logging.getLogger(__name__)

HEADER_MARKERS = ("csp id", "csp code", "ko code")


class SheetSourceError(RuntimeError):
    pass


def _rows_to_dicts(raw_rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Find the header row (the tab has a banner row above it) and zip each
    data row against it."""
    header_idx = None
    for i, row in enumerate(raw_rows[:10]):
        cells = [str(c).strip().lower() for c in row if c is not None]
        if any(c in HEADER_MARKERS for c in cells):
            header_idx = i
            break
    if header_idx is None:
        raise SheetSourceError(f"No 'CSP ID' header row found in tab '{CALLING_SHEET_TAB}'.")

    headers = [str(c).strip() if c is not None else "" for c in raw_rows[header_idx]]
    out = []
    for row in raw_rows[header_idx + 1:]:
        row = list(row) + [None] * (len(headers) - len(row))
        record = {h: row[j] for j, h in enumerate(headers) if h}
        if any(v not in (None, "") for v in record.values()):
            out.append(record)
    return out


def _read_local_xlsx(path: str) -> list[list[Any]]:
    import openpyxl

    if not os.path.exists(path):
        raise SheetSourceError(f"Calling sheet file not found: {path}")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        if CALLING_SHEET_TAB not in wb.sheetnames:
            raise SheetSourceError(f"Tab '{CALLING_SHEET_TAB}' not found in {path}.")
        ws = wb[CALLING_SHEET_TAB]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _read_google(sheet_id: str) -> list[list[Any]]:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    if not sheet_id:
        raise SheetSourceError("CALLING_SHEET_SPREADSHEET_ID is not set.")
    # A service account shared (Viewer) on this one spreadsheet; separate from
    # credentials.json, which is the Gmail login.
    creds_file = os.getenv("SHEETS_SERVICE_ACCOUNT_FILE", "sheets_service_account.json")
    if not os.path.exists(creds_file):
        raise SheetSourceError(f"Service-account file not found: {creds_file}")
    with open(creds_file) as f:
        if json.load(f).get("type") != "service_account":
            raise SheetSourceError("Live sheet access needs a service-account credentials file.")
    creds = service_account.Credentials.from_service_account_file(
        creds_file, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"'{CALLING_SHEET_TAB}'").execute()   # this tab only, every column
    return result.get("values", [])


def parse_sheet_link(link: str) -> tuple[str, str]:
    """(spreadsheet id, tab gid) from a Google Sheets link; "" where absent."""
    sid = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", link or "")
    gid = re.search(r"[#?&]gid=(\d+)", link or "")
    return (sid.group(1) if sid else "", gid.group(1) if gid else "")


def _read_link_csv(sheet_id: str, gid: str) -> list[list[Any]]:
    """One tab (gid) of a sheet shared "anyone with the link can view"."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            kind = r.headers.get("Content-Type", "")
            body = r.read().decode("utf-8-sig")
    except Exception as e:
        raise SheetSourceError(f"live sheet not reachable by link: {type(e).__name__}") from e
    if "text/csv" not in kind:
        raise SheetSourceError("the link is not shared for viewing (Google asked for a sign-in)")
    return [row for row in csv.reader(io.StringIO(body))]


def _read_live() -> tuple[str, list[list[Any]]]:
    sid, gid = parse_sheet_link(CALLING_SHEET_LINK)
    sid = sid or CALLING_SHEET_SPREADSHEET_ID
    if not sid:
        raise SheetSourceError("CALLING_SHEET_LINK has no spreadsheet id")
    errors = []
    if gid:
        try:
            return "link", _read_link_csv(sid, gid)
        except SheetSourceError as e:
            errors.append(str(e))
    else:
        errors.append("the link has no #gid= (open the 'Calling Sheet New' tab and copy the link again)")
    try:
        return "google", _read_google(sid)
    except (SheetSourceError, OSError) as e:
        errors.append(f"service account: {e}")
    raise SheetSourceError("; ".join(errors))


# What the last load used, for the dashboard and logs.
LAST_SOURCE: dict[str, str] = {}


def load_calling_sheet_rows() -> list[dict[str, Any]]:
    """Rows of the calling-sheet tab as dicts. Raises SheetSourceError."""
    source = CALLING_SHEET_SOURCE.strip().lower()
    if CALLING_SHEET_LINK:
        try:
            source, raw = _read_live()
            rows = _rows_to_dicts(raw)
            LAST_SOURCE.update(source=f"live sheet ({source})", note="")
            logger.info("calling_sheet_loaded source=%s tab=%s rows=%d", source, CALLING_SHEET_TAB, len(rows))
            return rows
        except SheetSourceError as e:
            logger.warning("calling_sheet_live_unavailable, using the saved copy: %s", e)
            LAST_SOURCE.update(source="saved copy", note=f"live sheet not read: {e}")
            source = "local_xlsx"
    else:
        LAST_SOURCE.update(source=source, note="")
    if source == "google":
        raw = _read_google(CALLING_SHEET_SPREADSHEET_ID)
    elif source == "local_xlsx":
        raw = _read_local_xlsx(CALLING_SHEET_LOCAL_PATH)
    else:
        raise SheetSourceError(f"Unknown CALLING_SHEET_SOURCE '{CALLING_SHEET_SOURCE}'.")
    rows = _rows_to_dicts(raw)
    logger.info("calling_sheet_loaded source=%s tab=%s rows=%d", source, CALLING_SHEET_TAB, len(rows))
    return rows
