"""秘書くん - Slack event handlers.

Uses slack_bolt AsyncApp in Socket Mode.
"""

from __future__ import annotations

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from agents.hisho.brain import Brain
from agents.hisho.calendar_manager import CalendarManager
from agents.hisho.config import Settings
from agents.hisho.email_triage import EmailTriage
from agents.hisho.reporter import Reporter

logger = logging.getLogger(__name__)


def create_slack_app(
    settings: Settings,
    brain: Brain,
    email_triage: EmailTriage,
    calendar_mgr: CalendarManager,
    reporter: Reporter,
) -> AsyncApp:
    """Create and configure the Slack bot."""
    app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    channel = settings.slack_channel_egao

    # --- Helpers ---

    async def send(client: AsyncWebClient, text: str, thread_ts: str | None = None):
        await client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )

    async def notify(text: str, thread_ts: str | None = None):
        """Notification function passed to EmailTriage."""
        client = AsyncWebClient(token=settings.slack_bot_token)
        await send(client, text, thread_ts)

    # Wire the notify function into email_triage
    email_triage._notify = notify

    # --- Event Handlers ---

    @app.event("app_mention")
    async def handle_mention(event, client):
        """Respond to @秘書くん mentions."""
        text = event.get("text", "")
        thread_ts = event.get("ts")

        # Strip bot mention
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        if not text:
            await send(client, "なに？呼んだ？ 😊", thread_ts)
            return

        response = await _route_command(text, client, thread_ts)
        await send(client, response, thread_ts)

    @app.event("message")
    async def handle_dm(event, client):
        """Handle direct messages."""
        # Only respond to DMs (channel type 'im')
        if event.get("channel_type") != "im":
            return
        # Ignore bot's own messages
        if event.get("bot_id"):
            return

        text = event.get("text", "")
        thread_ts = event.get("ts")

        response = await _route_command(text, client, thread_ts)
        await client.chat_postMessage(
            channel=event["channel"],
            text=response,
            thread_ts=thread_ts,
        )

    # --- Command Router ---

    async def _route_command(
        text: str, client: AsyncWebClient, thread_ts: str | None
    ) -> str:
        """Route a message to the appropriate handler."""
        text_lower = text.lower()

        # Email commands
        if any(kw in text_lower for kw in ["メール", "mail", "email"]):
            if any(kw in text_lower for kw in ["確認", "チェック", "check"]):
                return await email_triage.force_check()

        # Schedule commands
        if any(kw in text_lower for kw in ["スケジュール", "予定", "schedule", "カレンダー"]):
            if any(kw in text_lower for kw in ["明日", "tomorrow"]):
                import datetime as dt
                import pytz
                tz = pytz.timezone("Asia/Tokyo")
                tomorrow = (dt.datetime.now(tz) + dt.timedelta(days=1)).strftime("%Y-%m-%d")
                return reporter.generate_schedule_report(tomorrow)
            return reporter.generate_schedule_report()

        # Morning report
        if any(kw in text_lower for kw in ["朝の報告", "morning", "報告", "まとめ"]):
            return reporter.generate_morning_report()

        # Meeting scheduling (from sales-kun or direct)
        if any(kw in text_lower for kw in ["デモ設定", "商談設定", "ミーティング設定", "会議設定"]):
            result = calendar_mgr.schedule_meeting(text)
            return result.message

        # General: let brain handle it
        return brain.respond_to_message(text)

    return app


async def start_slack_app(app: AsyncApp, settings: Settings) -> None:
    """Start the Slack app in Socket Mode."""
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    await handler.start_async()
