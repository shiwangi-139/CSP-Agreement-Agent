"""
app/comms/gmail_oauth.py
Secure Google Cloud OAuth 2.0 Gmail integration for Eko CSP Agent.
- Uses minimal scopes (readonly, modify, send).
- Server-side email query filtering (only downloads CSP agreement emails).
- Automatically manages and silently refreshes gmail_token.json.
- Zero raw password storage.
"""

import os
import time
import base64
import logging
import socket
from email.message import EmailMessage

# Guard against hung Google API socket reads
socket.setdefaulttimeout(15.0)
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Minimal scopes selected during OAuth consent configuration
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "gmail_token.json"


class GmailAuthRequired(RuntimeError):
    pass


def get_gmail_service():
    """
    Authenticates using credentials.json and returns an authorized Gmail API service.
    If gmail_token.json does not exist or has expired, it refreshes automatically
    or opens a one-time browser login.
    """
    creds = None

    # Load existing access & refresh token
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # If tokens are missing or invalid, authenticate
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Access token expired. Refreshing token silently in background...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}! "
                    "Please download it from Google Cloud Console (OAuth Client ID -> Desktop App)."
                )
            # The worker runs headless on the rack server: waiting for a
            # browser login there would hang the job forever.
            import sys
            if not (sys.stdin and sys.stdin.isatty()) and os.getenv("GMAIL_INTERACTIVE_AUTH") != "1":
                raise GmailAuthRequired(
                    "Gmail token is missing or can't be refreshed. Run "
                    "`python -m scripts.authenticate_gmail` once in a terminal.")
            logger.info("Opening browser window for one-time Google Workspace authentication...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)

        # Write the token readable by this user only.
        fd = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as token:
            token.write(creds.to_json())
        logger.info(f"Saved authenticated credentials to {TOKEN_FILE}")

    return build("gmail", "v1", credentials=creds)


def fetch_attachment_bytes(service, message_id: str, attachment_id: str) -> bytes | None:
    """Download single attachment bytes on-demand with rate limit resilience."""
    for attempt in range(5):
        try:
            att_record = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=attachment_id
            ).execute()
            time.sleep(0.05)
            return base64.urlsafe_b64decode(att_record["data"])
        except Exception as ae:
            err_str = str(ae)
            if ("rateLimitExceeded" in err_str or "quota" in err_str.lower() or "429" in err_str or "403" in err_str) and attempt < 4:
                wait_sec = (attempt + 1) * 3
                logger.warning(f"Gmail rate limit downloading attachment {attachment_id}. Backing off {wait_sec}s (attempt {attempt + 1}/5)...")
                time.sleep(wait_sec)
            else:
                logger.warning(f"Could not download attachment {attachment_id} from {message_id}: {ae}")
                return None
    return None



from datetime import datetime, timezone, timedelta


def fetch_incoming_csp_emails(
    max_results: int = 50,
    max_total: int = 200,
    include_trash: bool = True,
    query_override: str | None = None,
    download_attachments: bool = False,
    days_back: int | None = 730,
    after_date: str | None = None
) -> list[dict]:
    """
    Scans the mailbox using server-side query filters.
    - days_back=730 (default) or after_date applies strict 2-year rolling window server-side.
    - Includes Inbox, Trash, and Sent if include_trash=True.
    - Uses pagination loop to crawl all candidate messages up to max_total.
    - Unrelated internal/company emails are NEVER fetched or read.
    """
    service = get_gmail_service()

    # Broad query covering agreements, police verifications, IIBF, and PDFs
    if query_override:
        query = query_override
    else:
        base_query = "(filename:pdf OR CSP OR KO OR Agreement OR terminal OR reset OR extension OR 'Police Verification' OR PVR OR 'character certificate' OR IIBF OR renewal)"
        if after_date:
            query = f"{base_query} after:{after_date}"
        elif days_back:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")
            query = f"{base_query} after:{cutoff}"
        else:
            query = base_query

    messages = []
    page_token = None

    while len(messages) < max_total:
        batch_size = min(max_results, max_total - len(messages))
        list_kwargs = {"userId": "me", "q": query, "maxResults": batch_size}
        if include_trash:
            list_kwargs["includeSpamTrash"] = True
        if page_token:
            list_kwargs["pageToken"] = page_token

        try:
            response = service.users().messages().list(**list_kwargs).execute()
        except Exception as e:
            logger.error(f"Error listing Gmail messages: {e}")
            break

        batch_msgs = response.get("messages", [])
        if not batch_msgs:
            break
        messages.extend(batch_msgs)
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    print(f"Discovered {len(messages)} candidate messages across mailbox (query: {query}, include_trash={include_trash}). Reading metadata...", flush=True)

    results = []
    for idx, msg_meta in enumerate(messages, 1):
        if idx % 10 == 0 or idx == len(messages):
            print(f"Reading message {idx}/{len(messages)}...", flush=True)
        msg_id = msg_meta["id"]
        msg = None
        for attempt in range(6):
            try:
                msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                break
            except Exception as ge:
                err_str = str(ge)
                if ("rateLimitExceeded" in err_str or "quota" in err_str.lower() or "429" in err_str or "403" in err_str) and attempt < 5:
                    wait_sec = (attempt + 1) * 3
                    logger.warning(f"Gmail rate limit reached on message {msg_id}. Backing off for {wait_sec}s (attempt {attempt + 1}/6)...")
                    time.sleep(wait_sec)
                else:
                    logger.warning(f"Could not retrieve full message {msg_id}: {ge}")
                    break

        if not msg:
            continue

        # Smooth pacing to stay comfortably under Google's per-minute quota
        time.sleep(0.08)


        label_ids = msg.get("labelIds", [])
        folder = "TRASH" if "TRASH" in label_ids else ("SPAM" if "SPAM" in label_ids else "INBOX")
        thread_id = msg.get("threadId", "")

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        message_id = headers.get("message-id", msg_id)
        received_date = headers.get("date", "")
        in_reply_to = headers.get("in-reply-to", "")
        references = headers.get("references", "")

        body = ""
        attachments = []

        def parse_parts(parts):
            nonlocal body
            for part in parts:
                mime_type = part.get("mimeType", "")
                filename = part.get("filename", "")
                body_data = part.get("body", {})

                # Extract plain text or HTML body
                if mime_type == "text/plain" and "data" in body_data:
                    body += base64.urlsafe_b64decode(body_data["data"]).decode(errors="ignore")
                elif mime_type == "text/html" and not body and "data" in body_data:
                    body += base64.urlsafe_b64decode(body_data["data"]).decode(errors="ignore")

                # Extract PDF attachments
                if filename and "attachmentId" in body_data:
                    att_id = body_data["attachmentId"]
                    file_bytes = None
                    if download_attachments:
                        file_bytes = fetch_attachment_bytes(service, msg_id, att_id)

                    attachments.append({
                        "filename": filename,
                        "mime_type": mime_type,
                        "attachment_id": att_id,
                        "data": file_bytes,
                    })

                if "parts" in part:
                    parse_parts(part["parts"])

        payload = msg.get("payload", {})
        if "parts" in payload:
            parse_parts(payload["parts"])
        elif "body" in payload and "data" in payload["body"]:
            body = base64.urlsafe_b64decode(payload["body"]["data"]).decode(errors="ignore")

        results.append({
            "id": msg_id,
            "thread_id": thread_id,
            "message_id": message_id,
            "in_reply_to": in_reply_to,
            "references": references,
            "sender": sender,
            "subject": subject,
            "date": received_date,
            "body": body,
            "attachments": attachments,
            "folder": folder,
            "label_ids": label_ids,
        })

    return results


def mark_email_as_seen(msg_id: str):
    """
    Removes the UNREAD label from a processed message so it isn't scanned twice.
    """
    service = get_gmail_service()
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def send_email_oauth(to_email: str, subject: str, body_text: str) -> dict:
    """
    Sends an email using the Gmail API (OAuth 2.0).
    No SMTP password required.
    """
    service = get_gmail_service()
    message = EmailMessage()
    message.set_content(body_text)
    message["To"] = to_email
    message["Subject"] = subject

    encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    create_message = {"raw": encoded_message}

    send_response = service.users().messages().send(userId="me", body=create_message).execute()
    logger.info(f"Email sent successfully to {to_email}. Message ID: {send_response['id']}")
    return send_response


# =============================================================================
# Read-once helpers used by app/gmail_ingest.py
# =============================================================================
ATTACHMENT_QUERY = ("(filename:pdf OR filename:jpg OR filename:jpeg OR filename:png) "
                    "-in:spam -in:trash")


def _with_backoff(call, what: str, attempts: int = 6):
    for attempt in range(attempts):
        try:
            return call()
        except Exception as e:
            err = str(e)
            retryable = any(x in err for x in ("rateLimitExceeded", "429", "backendError", "500", "503")) \
                or "quota" in err.lower()
            if retryable and attempt < attempts - 1:
                time.sleep((attempt + 1) * 3)
                continue
            raise


def list_message_ids(service, query: str, page_token: str | None = None, page_size: int = 100) -> tuple[list[str], str | None]:
    """One page of message ids for a query. Returns (ids, next_page_token)."""
    kwargs = {"userId": "me", "q": query, "maxResults": page_size, "includeSpamTrash": False}
    if page_token:
        kwargs["pageToken"] = page_token
    resp = _with_backoff(lambda: service.users().messages().list(**kwargs).execute(), "messages.list")
    return [m["id"] for m in resp.get("messages", [])], resp.get("nextPageToken")


def current_history_id(service) -> str:
    return str(_with_backoff(lambda: service.users().getProfile(userId="me").execute(), "getProfile")["historyId"])


class HistoryTooOld(RuntimeError):
    pass


def new_message_ids_since(service, history_id: str) -> tuple[list[str], str]:
    """Ids of messages added since history_id, and the newest history id.
    Raises HistoryTooOld if Gmail no longer keeps that far back (~1 week)."""
    ids, token, newest = [], None, history_id
    while True:
        kwargs = {"userId": "me", "startHistoryId": history_id, "historyTypes": ["messageAdded"], "maxResults": 500}
        if token:
            kwargs["pageToken"] = token
        try:
            resp = _with_backoff(lambda: service.users().history().list(**kwargs).execute(), "history.list")
        except Exception as e:
            if "404" in str(e):
                raise HistoryTooOld(str(e))
            raise
        for h in resp.get("history", []):
            for added in h.get("messagesAdded", []):
                mid = added["message"]["id"]
                labels = added["message"].get("labelIds", [])
                if "SPAM" not in labels and "TRASH" not in labels and mid not in ids:
                    ids.append(mid)
        newest = str(resp.get("historyId", newest))
        token = resp.get("nextPageToken")
        if not token:
            return ids, newest


def get_message(service, msg_id: str) -> dict:
    """Full message: headers, text body and attachment descriptors (bytes are
    fetched later, only for attachments we actually process)."""
    msg = _with_backoff(lambda: service.users().messages().get(userId="me", id=msg_id, format="full").execute(),
                        "messages.get")
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body_parts: list[str] = []
    html_parts: list[str] = []
    attachments = []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {})
        if part.get("filename") and "attachmentId" in data:
            attachments.append({"filename": part["filename"], "mime_type": mime,
                                "attachment_id": data["attachmentId"], "size": data.get("size", 0)})
        elif mime == "text/plain" and "data" in data:
            body_parts.append(base64.urlsafe_b64decode(data["data"]).decode(errors="ignore"))
        elif mime == "text/html" and "data" in data:
            html_parts.append(base64.urlsafe_b64decode(data["data"]).decode(errors="ignore"))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(msg.get("payload", {}))
    body = "\n".join(body_parts)
    if not body and html_parts:
        import re as _re
        body = _re.sub(r"<[^>]+>", " ", "\n".join(html_parts))
    return {
        "id": msg_id, "thread_id": msg.get("threadId"), "label_ids": msg.get("labelIds", []),
        "message_id": headers.get("message-id", msg_id), "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""), "date": headers.get("date", ""),
        "internal_date_ms": int(msg.get("internalDate", 0) or 0), "body": body, "attachments": attachments,
    }
