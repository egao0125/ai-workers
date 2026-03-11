"""秘書くん - Gmail API client.

Safety invariant: This client can read emails and create drafts.
It has NO send() method. The OAuth scope (gmail.modify) does not
permit sending via messages.send().
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from agents.hisho.config import Settings

logger = logging.getLogger(__name__)

# gmail.modify: read, draft, label, mark-as-read. Does NOT allow send.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    subject: str
    sender: str
    sender_email: str
    date: str
    snippet: str
    body_text: str
    labels: list[str] = field(default_factory=list)
    is_unread: bool = True


class GmailClient:
    """Gmail API wrapper. Read + draft only. No send capability."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._service = None

    def _get_credentials(self) -> Credentials:
        creds = None
        token_path = Path(self._settings.gmail_token_path)
        creds_path = Path(self._settings.gmail_credentials_path)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not creds_path.exists():
                    raise FileNotFoundError(
                        f"OAuth credentials not found at {creds_path}. "
                        "Download from Google Cloud Console."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(creds_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        return creds

    @property
    def service(self):
        if self._service is None:
            creds = self._get_credentials()
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def list_unread(self, max_results: int = 20) -> list[EmailMessage]:
        """Fetch unread emails from inbox."""
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
            logger.exception("Failed to list unread emails")
            return []

        messages = results.get("messages", [])
        emails = []
        for msg_meta in messages:
            email = self.get_message(msg_meta["id"])
            if email:
                emails.append(email)
        return emails

    def get_message(self, msg_id: str) -> EmailMessage | None:
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

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        body_text = self._extract_body(msg["payload"])

        sender_raw = headers.get("From", "")
        sender_name, sender_email = self._parse_sender(sender_raw)

        return EmailMessage(
            id=msg["id"],
            thread_id=msg["threadId"],
            subject=headers.get("Subject", "(no subject)"),
            sender=sender_name,
            sender_email=sender_email,
            date=headers.get("Date", ""),
            snippet=msg.get("snippet", ""),
            body_text=body_text[:3000],  # Truncate for Claude context
            labels=msg.get("labelIds", []),
            is_unread="UNREAD" in msg.get("labelIds", []),
        )

    def create_draft(
        self,
        to: str,
        subject: str,
        body: str,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Create a draft email. NEVER sends.

        Returns the draft resource dict.
        """
        message = MIMEText(body, "plain", "utf-8")
        message["to"] = to
        message["subject"] = subject

        draft_body: dict = {
            "message": {
                "raw": base64.urlsafe_b64encode(
                    message.as_bytes()
                ).decode("ascii"),
            }
        }

        if reply_to_message_id:
            draft_body["message"]["threadId"] = reply_to_message_id

        try:
            draft = (
                self.service.users()
                .drafts()
                .create(userId="me", body=draft_body)
                .execute()
            )
            logger.info("Draft created: %s", draft.get("id"))
            return draft
        except Exception:
            logger.exception("Failed to create draft")
            return {}

    def mark_as_read(self, msg_id: str) -> None:
        """Remove UNREAD label from a message."""
        try:
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception:
            logger.exception("Failed to mark message %s as read", msg_id)

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from message payload."""
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get(
            "data"
        ):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )

        for part in payload.get("parts", []):
            text = self._extract_body(part)
            if text:
                return text

        return ""

    @staticmethod
    def _parse_sender(raw: str) -> tuple[str, str]:
        """Parse 'Name <email>' format."""
        if "<" in raw and ">" in raw:
            name = raw.split("<")[0].strip().strip('"')
            email = raw.split("<")[1].split(">")[0]
            return name, email
        return raw, raw


def setup_oauth():
    """One-time OAuth setup. Run: python -m agents.hisho.gmail_client"""
    from agents.hisho.config import get_settings

    settings = get_settings()
    client = GmailClient(settings)
    # This triggers the OAuth flow if token.json doesn't exist
    _ = client.service
    print("OAuth setup complete. token.json created.")


if __name__ == "__main__":
    setup_oauth()
