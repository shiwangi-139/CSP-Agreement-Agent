"""
app/comms/sheets_sync.py
Loads the calling sheet ("Calling Sheet New" only) into the database.

The sheet is the master record for CSP, RM and DC contact details, so every
load overwrites contact fields with what the sheet says. Rows come from
app/comms/sheet_source.py (local xlsx while testing, live sheet at
deployment); this module never downloads or writes any file.
"""
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import CSP, InternalUser
from ..validation import normalize_csp_code
from .sheet_source import load_calling_sheet_rows, SheetSourceError

logger = logging.getLogger(__name__)

CSP_CODE_RE = re.compile(r"^\d[A-Z]\d{6}$")
UNASSIGNED = {"", "tba", "na", "n/a", "none", "-", "nil", "not assigned", "vacant"}

# Header text in the tab -> our field. Matched case-insensitively after
# trimming, because the sheet has stray trailing spaces ("CSP ID ").
COLUMNS = {
    "csp id": "code",
    "csp name": "name",
    "csp mail id": "email",
    "csp mobile number": "phone",
    "alternative mobile number": "alt_phone",
    "relationship manager": "rm_name",
    "mobile no of rm": "rm_phone",
    "email of rm": "rm_email",
    "district coordinator": "dc_name",
    "mobile number dc": "dc_phone",
    "email id dc": "dc_email",
    "branch code": "branch_code",
    "branch name": "branch",
    "state": "state",
    "circle (lho)": "circle",
    "terminal status": "terminal_status",
    "full address": "address",
}


def generate_row_hash(row_data: list) -> str:
    """SHA-256 of a sheet row, used to skip rows that haven't changed."""
    row_str = json.dumps([str(x).strip() for x in row_data])
    return hashlib.sha256(row_str.encode("utf-8")).hexdigest()


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def clean_phones(v: Any) -> list[str]:
    """All valid Indian mobile numbers in a cell, as 10 digits.

    Handles "9876543210.0" (numbers stored as decimals), "98395 00750",
    "+91 98...", and cells holding two numbers ("a/ b", "a \\ b").
    Text like "Replace" or "Inactive" yields [].
    """
    s = _text(v)
    if not s:
        return []
    out = []
    for part in re.split(r"[/\\,;|]+", s):
        digits = re.sub(r"\D", "", part)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "6789" and digits not in out:
            out.append(digits)
    return out


def clean_email(v: Any) -> str | None:
    s = _text(v).lower().strip(" ,;")
    s = re.split(r"[\s,;/]+", s)[0] if s else ""
    return s if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", s) else None


def _is_unassigned(name: str) -> bool:
    return name.strip().lower() in UNASSIGNED


def parse_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one sheet row to clean fields, or None if it isn't a CSP row."""
    row = {}
    for header, value in raw.items():
        key = COLUMNS.get(header.strip().lower())
        if key:
            row[key] = value
    code = normalize_csp_code(_text(row.get("code")).upper())
    if not CSP_CODE_RE.match(code or ""):
        return None
    phones = clean_phones(row.get("phone")) + clean_phones(row.get("alt_phone"))
    dc_phones = clean_phones(row.get("dc_phone"))
    rm_phones = clean_phones(row.get("rm_phone"))
    return {
        "code": code,
        "name": _text(row.get("name")) or f"CSP {code}",
        "email": clean_email(row.get("email")),
        "phone": phones[0] if phones else None,
        "alt_phone": phones[1] if len(phones) > 1 else None,
        "rm_name": _text(row.get("rm_name")),
        "rm_phone": rm_phones[0] if rm_phones else None,
        "rm_email": clean_email(row.get("rm_email")),
        "dc_name": _text(row.get("dc_name")),
        "dc_phone": dc_phones[0] if dc_phones else None,
        "dc_email": clean_email(row.get("dc_email")),
        "branch_code": _text(row.get("branch_code")) or None,
        "branch": _text(row.get("branch")) or None,
        "state": _text(row.get("state")) or None,
        "circle": _text(row.get("circle")) or None,
        "terminal_status": _text(row.get("terminal_status")) or None,
        "kiosk_location": _text(row.get("address")) or None,
        "row_hash": generate_row_hash([raw.get(k) for k in sorted(raw)]),
    }


def compute_contact_gaps(csp: CSP, rm: InternalUser | None, dc: InternalUser | None) -> dict[str, list[str]]:
    gaps = {"csp": [], "rm": [], "dc": []}
    if not csp.phone:
        gaps["csp"].append("phone_missing")
    if not csp.email:
        gaps["csp"].append("email_missing")
    for role, user in (("rm", rm), ("dc", dc)):
        if user is None:
            gaps[role].append("not_assigned")
            continue
        if not user.phone:
            gaps[role].append("phone_missing")
        if not user.email:
            gaps[role].append("email_missing")
    return gaps


def _upsert_staff(db: Session, role: str, names_to_rows: dict[str, list[dict]]) -> dict[str, InternalUser]:
    """One InternalUser per RM/DC name. Their phone and email are the most
    common non-empty values across their rows in the sheet ("Mobile No Of
    RM" / "Email of RM", "Mobile Number DC" / "Email ID DC"). A blank in the
    sheet never erases a contact an admin entered."""
    existing = {u.name.strip().lower(): u for u in db.query(InternalUser).filter(InternalUser.role == role)}
    taken_emails = {e for (e,) in db.query(InternalUser.email).filter(InternalUser.email.isnot(None))}
    out = {}
    for name, rows in names_to_rows.items():
        key = name.strip().lower()
        user = existing.get(key)
        if user is None:
            user = InternalUser(name=name.strip(), role=role)
            db.add(user)
        prefix = "dc" if role == "DC" else "rm"
        phones = Counter(r[f"{prefix}_phone"] for r in rows if r.get(f"{prefix}_phone"))
        emails = Counter(r[f"{prefix}_email"] for r in rows if r.get(f"{prefix}_email"))
        if phones:
            user.phone = phones.most_common(1)[0][0]
        # The email is also the dashboard login: once someone can log in with
        # it, the sheet doesn't change it.
        if emails and not (user.login_enabled and user.email):
            email = emails.most_common(1)[0][0]
            if email == user.email or email not in taken_emails:
                user.email = email
                taken_emails.add(email)
        out[key] = user
    db.flush()
    return out


def sync_calling_sheet(sheet_id: str | None = None, range_name: str | None = None) -> dict:
    """Load the calling sheet into the DB. Arguments are kept for backward
    compatibility and ignored: the source and tab come from config."""
    summary = {"total_rows": 0, "inserted": 0, "updated": 0, "unchanged": 0,
               "skipped_invalid": 0, "deactivated": 0, "errors": 0}
    try:
        raw_rows = load_calling_sheet_rows()
    except (SheetSourceError, OSError) as e:
        logger.error("calling_sheet_load_failed: %s", e)
        summary["errors"] += 1
        summary["error"] = str(e)
        return summary

    parsed = []
    for raw in raw_rows:
        row = parse_row(raw)
        if row is None:
            summary["skipped_invalid"] += 1
        else:
            parsed.append(row)
    summary["total_rows"] = len(parsed)

    db: Session = SessionLocal()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        rm_rows, dc_rows = defaultdict(list), defaultdict(list)
        for r in parsed:
            if not _is_unassigned(r["rm_name"]):
                rm_rows[r["rm_name"]].append(r)
            if not _is_unassigned(r["dc_name"]):
                dc_rows[r["dc_name"]].append(r)
        rms = _upsert_staff(db, "RM", rm_rows)
        dcs = _upsert_staff(db, "DC", dc_rows)

        by_code = {c.lookup_code: c for c in db.query(CSP).all() if c.lookup_code}
        seen = set()
        for r in parsed:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            csp = by_code.get(r["code"])
            rm = rms.get(r["rm_name"].strip().lower())
            dc = dcs.get(r["dc_name"].strip().lower())
            if csp is None:
                csp = CSP(lookup_code=r["code"], current_code=r["code"], name=r["name"])
                db.add(csp)
                summary["inserted"] += 1
            elif csp.sheet_row_version == r["row_hash"] and csp.is_active_in_calling_sheet:
                summary["unchanged"] += 1
            else:
                summary["updated"] += 1

            csp.name = r["name"]
            csp.email = r["email"]
            csp.phone = r["phone"]
            csp.whatsapp_number = r["phone"]
            csp.alt_phone = r["alt_phone"]
            csp.branch = r["branch"]
            csp.branch_code = r["branch_code"]
            csp.state = r["state"]
            csp.region = r["state"]
            csp.circle = r["circle"]
            csp.terminal_status = r["terminal_status"]
            csp.kiosk_location = r["kiosk_location"]
            csp.status = (r["terminal_status"] or "ACTIVE").upper()
            csp.rm_id = rm.id if rm else None
            csp.dc_id = dc.id if dc else None
            csp.is_active_in_calling_sheet = True
            csp.sheet_row_version = r["row_hash"]
            csp.calling_sheet_synced_at = now
            csp.contact_gaps = compute_contact_gaps(csp, rm, dc)
            csp.has_missing_contact = bool(csp.contact_gaps["csp"])

        active = sum(1 for c in by_code.values() if c.is_active_in_calling_sheet)
        if active and len(seen) < active * 0.5:
            # A wrong tab or a half-loaded sheet must not switch off half the
            # CSPs: change nothing and say why.
            raise SheetSourceError(f"only {len(seen)} CSPs read but {active} are active: "
                                   "not applying this load (wrong tab or incomplete sheet?)")
        for code, csp in by_code.items():
            if code not in seen and csp.is_active_in_calling_sheet:
                csp.is_active_in_calling_sheet = False
                summary["deactivated"] += 1

        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("calling_sheet_sync_failed")
        summary["errors"] += 1
        summary["error"] = str(e)[:300]
    finally:
        db.close()

    from .sheet_source import LAST_SOURCE
    summary["source"] = LAST_SOURCE.get("source", "")
    if LAST_SOURCE.get("note"):
        summary["note"] = LAST_SOURCE["note"]
    logger.info("calling_sheet_sync_complete %s", summary)
    return summary


def refresh_contact_gaps(db: Session) -> None:
    """Recompute gaps after an admin edits RM/DC contacts on the dashboard."""
    users = {u.id: u for u in db.query(InternalUser).all()}
    for csp in db.query(CSP).filter(CSP.is_active_in_calling_sheet.is_(True)):
        csp.contact_gaps = compute_contact_gaps(csp, users.get(csp.rm_id), users.get(csp.dc_id))
        csp.has_missing_contact = bool(csp.contact_gaps["csp"])
