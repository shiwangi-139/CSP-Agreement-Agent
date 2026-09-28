import asyncio
import smtplib
import logging
from email.mime.text import MIMEText
from ..config import EMAIL_USER, EMAIL_APP_PASSWORD, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT
from .base import NotificationProvider

logger = logging.getLogger(__name__)

TEMPLATES = {
    "renewal_reminder": (
        "Agreement Renewal Reminder - {csp_code}",
        "Dear {csp_name},\n\nYour agreement (code {csp_code}) expires on {expiry_date}. "
        "Please submit your renewal documents at your earliest convenience.\n\nRegards,\nCompliance Team",
    ),
    "critical_escalation": (
        "URGENT: Agreement Expiring - {csp_code}",
        "This is a critical notice: agreement {csp_code} expires on {expiry_date} "
        "and has not been renewed. Immediate action is required.",
    ),
    "document_request": (
        "CSP Agreement - {csp_code} - Document Submission Required",
        "Dear {csp_name},\n\nAs part of the renewal process for CSP code {csp_code}, "
        "please submit the following document(s):\n\n{required_documents_list}\n\n"
        "Reply to this email with the document(s) attached. "
        "Submission deadline: {deadline}.\n\nRegards,\nCompliance Team",
    ),
    "missing_documents_followup": (
        "CSP Agreement - {csp_code} - Additional Document(s) Still Required",
        "Dear {csp_name},\n\nThank you for your submission for CSP code {csp_code}. "
        "We are still missing the following required document(s):\n\n{missing_documents_list}\n\n"
        "Please reply to this email with the remaining document(s) attached.\n\nRegards,\nCompliance Team",
    ),
}


class EmailAdapter(NotificationProvider):
    """Email first -- it's the easiest channel to test and audit, and it's
    genuinely free via Gmail SMTP at this volume.
    """

    async def send(self, recipient: str, template: str, variables: dict, idempotency_key: str) -> str:
        subject_tpl, body_tpl = TEMPLATES[template]
        subject = subject_tpl.format(**variables)
        body = body_tpl.format(**variables)

        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = recipient

        await asyncio.to_thread(self._send_blocking, recipient, msg)

        # This proves our SMTP server accepted the message, not that the
        # recipient received it. Real delivery/bounce tracking needs a
        # provider with webhooks or mailbox bounce processing -- out of
        # scope for the free-tier Gmail SMTP setup.
        logger.info("email_smtp_accepted", extra={"channel": "EMAIL", "reason": idempotency_key})
        return idempotency_key

    @staticmethod
    def _send_blocking(recipient: str, msg: MIMEText) -> None:
        with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_USER, [recipient], msg.as_string())
