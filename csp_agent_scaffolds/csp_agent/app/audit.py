"""
app/audit.py
Records each dashboard API request of a signed-in person (UserAction): what
was opened or done, by whom, when. The sidebar's own refresh (/api/hub/summary)
and the files themselves are not recorded. Never breaks a request: a failure
to record is only logged.
"""
import logging
import re
from datetime import datetime, timezone

from .db import SessionLocal

logger = logging.getLogger(__name__)
SKIP = ("/api/hub/summary",)
SESSION = None              # tests point this at the test database
CSP_IN_PATH = re.compile(r"/csp/(\d+)")

# What each address means, for the Activity page.
LABELS = [
    (r"^GET /api/hub/today", "Opened Today"), (r"^GET /api/hub/csps", "Opened the Labels list"),
    (r"^GET /api/hub/csp/\d+$", "Opened a CSP's page"), (r"^GET /api/hub/documents/\d+/file", "Viewed a document"),
    (r"^GET /api/hub/documents", "Opened Latest documents"), (r"^GET /api/hub/review", "Opened the Review queue"),
    (r"^POST /api/hub/review/\d+/resolve", "Decided a review item"), (r"^POST /api/hub/review/\d+/reread", "Re-read a document"),
    (r"^GET /api/hub/messages", "Opened the Communication Hub"), (r"^POST /api/hub/messages/.*/approve", "Approved a message"),
    (r"^POST /api/hub/messages/.*/reject", "Rejected a message"), (r"^POST /api/hub/messages/.*/send-now", "Sent a message now"),
    (r"^POST /api/hub/messages/.*/edit", "Edited a message"), (r"^GET /api/hub/questions", "Opened CSP questions"),
    (r"^POST /api/hub/questions/\d+/answer", "Answered a CSP question"), (r"^GET /api/hub/form-uploads", "Opened Form uploads"),
    (r"^POST /api/hub/form-uploads/\d+/take", "Took a refused file"), (r"^POST /api/hub/csp/\d+/send-reminder", "Sent a reminder"),
    (r"^POST /api/hub/csp/\d+/active", "Changed a CSP's active status"), (r"^POST /api/hub/csp/\d+/whatsapp-number", "Set a WhatsApp number"),
    (r"^POST /api/hub/csp/\d+/upload-link", "Made an upload link"), (r"^GET /api/hub/find", "Searched / filtered CSPs"),
    (r"^GET /api/hub/search", "Searched a CSP"), (r"^GET /api/hub/coach", "Opened the Agent Coach"),
    (r"^POST /api/hub/coach/ask", "Asked the Agent Coach"), (r"^GET /api/hub/reports/", "Downloaded a report"),
    (r"^GET /api/hub/dc-report", "Downloaded a DC Excel"), (r"^GET /api/hub/dc-summary", "Made a DC text summary"),
    (r"^POST /api/hub/run/", "Started a job"), (r"^GET /api/hub/monitor", "Opened Monitoring"),
]


def label(method: str, path: str) -> str:
    key = f"{method} {path}"
    return next((text for pat, text in LABELS if re.match(pat, key)), f"{method.title()} {path.replace('/api/hub/', '')}")


def record(request, status_code: int) -> None:
    path = request.url.path
    if not path.startswith("/api/hub/") or path.startswith(SKIP) or request.method == "OPTIONS":
        return
    from .auth import COOKIE, _hash_token
    token = request.cookies.get(COOKIE, "")
    if not token:
        return                                  # the server's own API-key calls are not a person
    db = (SESSION or SessionLocal)()
    try:
        from .models import AdminSession, InternalUser, UserAction
        s = db.get(AdminSession, _hash_token(token))
        user = db.get(InternalUser, s.user_id) if s else None
        if user is None:
            return
        m = CSP_IN_PATH.search(path)
        db.add(UserAction(at=datetime.now(timezone.utc).replace(tzinfo=None), user_id=user.id,
                          user_name=user.name or user.email, role=user.role, method=request.method, path=path[:300],
                          status_code=status_code, csp_id=int(m.group(1)) if m else None))
        db.commit()
    except Exception:
        logger.exception("audit_record_failed path=%s", path)
        db.rollback()
    finally:
        db.close()
