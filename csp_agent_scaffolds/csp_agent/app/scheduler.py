"""
Jobs now run in the separate worker process (python -m app.worker), not
inside the web server. This module stays for the /api/scheduler/status
route and older imports.
"""
from .config import APP_TIMEZONE, GMAIL_SCAN_INTERVAL_MINUTES


def get_scheduler_status() -> dict:
    return {
        "runs_in": "worker process (python -m app.worker)",
        "timezone": APP_TIMEZONE,
        "jobs": {
            "gmail": f"every {GMAIL_SCAN_INTERVAL_MINUTES} min",
            "sheet": "every 60 min",
            "engine": "daily 09:00",
            "outbox": "every 2 min",
            "vault": "daily 00:10 (expiry renames, INDEX.xlsx)",
            "reports": "daily 07:30 (CSP report + Contacts & gaps .xlsx)",
        },
    }


def setup_scheduler_jobs():
    """No-op: kept so older code that calls it keeps working."""
    return None
