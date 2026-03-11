"""秘書くん - Email triage orchestration.

Ties Gmail client + Brain together for the email classification workflow.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Awaitable

from agents.hisho.brain import Brain, TriageResult, DraftReply
from agents.hisho.gmail_client import GmailClient, EmailMessage

logger = logging.getLogger(__name__)

STATE_FILE = Path("processed_emails.json")


class EmailTriage:
    """Orchestrates email checking, classification, and notification."""

    def __init__(
        self,
        gmail: GmailClient,
        brain: Brain,
        notify_fn: Callable[[str, str | None], Awaitable[None]],
    ) -> None:
        self._gmail = gmail
        self._brain = brain
        self._notify = notify_fn  # async fn(text, thread_ts=None)
        self._processed: set[str] = self._load_state()

    def _load_state(self) -> set[str]:
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                return set(data.get("processed", []))
            except Exception:
                pass
        return set()

    def _save_state(self) -> None:
        # Keep only last 500 IDs to prevent unbounded growth
        recent = list(self._processed)[-500:]
        STATE_FILE.write_text(json.dumps({"processed": recent}))

    async def check_new_emails(self) -> list[tuple[EmailMessage, TriageResult]]:
        """Check for new emails, triage them, and notify as needed.

        Returns list of (email, triage_result) for all new emails.
        """
        emails = self._gmail.list_unread()
        results = []

        for email in emails:
            if email.id in self._processed:
                continue

            logger.info("New email: %s from %s", email.subject, email.sender)
            triage = self._brain.triage_email(email)
            results.append((email, triage))
            self._processed.add(email.id)

            # Handle based on priority
            if triage.priority == "red":
                await self._handle_red(email, triage)
            elif triage.priority == "yellow":
                await self._handle_yellow(email, triage)
            # Green: logged only, included in daily digest

        if results:
            self._save_state()

        return results

    async def _handle_red(
        self, email: EmailMessage, triage: TriageResult
    ) -> None:
        """Handle urgent (red) emails: notify immediately + draft reply."""
        # Notify Slack immediately
        notification = (
            f"🔴 *緊急メール*\n"
            f"*From:* {email.sender} ({email.sender_email})\n"
            f"*件名:* {email.subject}\n"
            f"*内容:* {triage.summary}\n"
            f"→ {triage.reason}"
        )

        if triage.needs_draft:
            draft = self._brain.draft_reply(email)
            gmail_draft = self._gmail.create_draft(
                to=draft.to,
                subject=draft.subject,
                body=draft.body,
                reply_to_message_id=email.thread_id,
            )
            draft_id = gmail_draft.get("id", "?")
            notification += (
                f"\n\n📩 下書き作っておいたよ (Draft ID: {draft_id})\n"
                f"Gmailの下書きフォルダから確認して送信してね"
            )

        await self._notify(notification, None)

    async def _handle_yellow(
        self, email: EmailMessage, triage: TriageResult
    ) -> None:
        """Handle important (yellow) emails: notify in thread."""
        notification = (
            f"🟡 {triage.summary}\n"
            f"From: {email.sender} | {triage.reason}"
        )
        await self._notify(notification, None)

    def get_summary(self) -> dict:
        """Get a summary of pending email status for reports."""
        emails = self._gmail.list_unread(max_results=50)
        red, yellow, green = [], [], []

        for email in emails:
            triage = self._brain.triage_email(email)
            if triage.priority == "red":
                red.append({"subject": email.subject, "sender": email.sender, "summary": triage.summary})
            elif triage.priority == "yellow":
                yellow.append({"subject": email.subject, "sender": email.sender, "summary": triage.summary})
            else:
                green.append({"subject": email.subject, "sender": email.sender})

        return {
            "red": red,
            "red_count": len(red),
            "yellow": yellow,
            "yellow_count": len(yellow),
            "green_count": len(green),
            "total_unread": len(emails),
        }

    async def force_check(self) -> str:
        """Force an immediate email check and return a formatted summary."""
        results = await self.check_new_emails()

        if not results:
            return "新しいメールはないよ ✨"

        lines = [f"📬 新着メール {len(results)}件：\n"]
        for email, triage in results:
            lines.append(f"{triage.emoji} {triage.summary}")

        return "\n".join(lines)
