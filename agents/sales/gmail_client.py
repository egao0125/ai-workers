"""セールスくん - Gmail API client for email intake.

Uses SERVICE ACCOUNT auth (not OAuth). Monitors contact@stepai.co.jp.
Ports gmail.ts from maakun.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from agents.sales.config import Settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class ParsedEmail:
    id: str
    thread_id: str
    from_raw: str
    from_name: str
    from_email: str
    subject: str
    body: str
    html_body: str
    received_at: str


@dataclass
class FramerFormData:
    name: str | None = None
    email: str | None = None
    company: str | None = None
    requirement: str | None = None
    referral_source: str | None = None
    message: str | None = None
    extra: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_base64url(data: str) -> str:
    """Decode base64url-encoded string."""
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _get_header(headers: list[dict[str, str]], name: str) -> str:
    """Get a header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _parse_from(raw: str) -> tuple[str, str]:
    """Parse 'Name <email>' format."""
    match = re.match(r'^(.+?)\s*<(.+?)>$', raw)
    if match:
        return match.group(1).strip().strip('"'), match.group(2).strip()
    return raw, raw


def _extract_bodies(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract text and HTML bodies from message payload."""
    text = ""
    html = ""

    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        text = _decode_base64url(body_data)
    if mime == "text/html" and body_data:
        html = _decode_base64url(body_data)

    for part in payload.get("parts", []):
        part_mime = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data", "")
        if not text and part_mime == "text/plain" and part_data:
            text = _decode_base64url(part_data)
        if not html and part_mime == "text/html" and part_data:
            html = _decode_base64url(part_data)

    # Fallback: strip HTML tags
    if not text and html:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()[:2000]

    return text, html


def parse_framer_form(html: str) -> FramerFormData | None:
    """Parse Framer form submission emails.

    Form fields are embedded as: <span style="color:#999999">LABEL: </span>VALUE
    """
    if not html:
        return None

    field_regex = re.compile(
        r'<span[^>]*color:\s*#999999[^>]*>([^<]+?):\s*</span>([^<]*)'
    )
    fields: dict[str, str] = {}

    for match in field_regex.finditer(html):
        label = match.group(1).strip()
        value = match.group(2).strip()
        if value:
            fields[label] = value

    if not fields:
        return None

    # Map Japanese/English field names to standard keys
    label_map = {
        "名前": "name", "Name": "name", "お名前": "name",
        "メールアドレス": "email", "Email": "email", "メール": "email",
        "会社名": "company", "Company": "company",
        "要件": "requirement", "ご要件": "requirement", "Requirement": "requirement",
        "本サービスを知ったきっかけ": "referral_source",
        "メッセージ": "message", "Message": "message", "お問い合わせ内容": "message",
    }

    result = FramerFormData()
    extra: dict[str, str] = {}

    for label, value in fields.items():
        key = label_map.get(label)
        if key and hasattr(result, key):
            setattr(result, key, value)
        else:
            extra[label] = value

    if extra:
        result.extra = extra

    return result


# ---------------------------------------------------------------------------
# Gmail Client
# ---------------------------------------------------------------------------

class GmailClient:
    """Gmail API client using service account auth."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = None

    def _build_service(self):
        """Build Gmail API service with service account credentials."""
        if not self._settings.gmail_client_email or not self._settings.gmail_private_key:
            raise RuntimeError("Gmail service account credentials not configured")

        # Build credentials from env vars (not a file)
        info = {
            "type": "service_account",
            "client_email": self._settings.gmail_client_email,
            "private_key": self._settings.gmail_private_key.replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=SCOPES,
            subject=self._settings.gmail_watch_email,
        )

        return build("gmail", "v1", credentials=creds)

    @property
    def service(self):
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def list_new_emails(self, max_results: int = 10) -> list[ParsedEmail]:
        """Fetch recent unread emails from inbox."""
        try:
            results = (
                self.service.users()
                .messages()
                .list(
                    userId="me",
                    q="is:unread in:inbox",
                    maxResults=max_results,
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to list new emails")
            return []

        messages_meta = results.get("messages", [])
        emails = []
        for meta in messages_meta:
            email = self.get_message(meta["id"])
            if email:
                emails.append(email)
        return emails

    def get_message(self, msg_id: str) -> ParsedEmail | None:
        """Fetch a single email with full body."""
        try:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except Exception:
            logger.exception("Failed to get message %s", msg_id)
            return None

        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        from_raw = _get_header(headers, "From")
        from_name, from_email = _parse_from(from_raw)
        text, html = _extract_bodies(payload)

        internal_date = msg.get("internalDate")
        if internal_date:
            from datetime import datetime, timezone
            received_at = datetime.fromtimestamp(
                int(internal_date) / 1000, tz=timezone.utc
            ).isoformat()
        else:
            from datetime import datetime, timezone
            received_at = datetime.now(timezone.utc).isoformat()

        return ParsedEmail(
            id=msg.get("id", msg_id),
            thread_id=msg.get("threadId", ""),
            from_raw=from_raw,
            from_name=from_name,
            from_email=from_email,
            subject=_get_header(headers, "Subject"),
            body=text,
            html_body=html,
            received_at=received_at,
        )

    def check_reply_status(
        self, thread_id: str, after_timestamp: str
    ) -> dict[str, Any]:
        """Check if someone from our side has replied to a thread."""
        try:
            res = (
                self.service.users()
                .threads()
                .get(
                    userId="me",
                    id=thread_id,
                    format="metadata",
                    metadataHeaders=["From", "Date"],
                )
                .execute()
            )

            messages = res.get("messages", [])
            watch_email = self._settings.gmail_watch_email
            from datetime import datetime, timezone

            after_ms = int(
                datetime.fromisoformat(after_timestamp).timestamp() * 1000
            )

            # Check if any message after the initial one was sent by us
            for msg in messages[1:]:
                headers = msg.get("payload", {}).get("headers", [])
                from_header = _get_header(headers, "From")
                internal_date = int(msg.get("internalDate", "0"))

                if internal_date > after_ms and watch_email in from_header:
                    return {
                        "replied": True,
                        "replied_at": datetime.fromtimestamp(
                            internal_date / 1000, tz=timezone.utc
                        ).isoformat(),
                    }

            return {"replied": False}

        except Exception:
            logger.exception("Failed to check reply status for thread %s", thread_id)
            return {"replied": False}
