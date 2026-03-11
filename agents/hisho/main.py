"""秘書くん - Entry point.

Wires all modules together and starts the bot.

Usage:
    python -m agents.hisho.main
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from agents.hisho.brain import Brain
from agents.hisho.calendar_client import CalendarClient
from agents.hisho.calendar_manager import CalendarManager
from agents.hisho.config import get_settings
from agents.hisho.email_triage import EmailTriage
from agents.hisho.gmail_client import GmailClient
from agents.hisho.reporter import Reporter
from agents.hisho.scheduler import setup_scheduler
from agents.hisho.slack_handler import create_slack_app, start_slack_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hisho")


async def main() -> None:
    logger.info("秘書くん starting up...")
    settings = get_settings()

    # --- Initialize clients ---
    gmail = GmailClient(settings)
    calendar = CalendarClient(settings)
    brain = Brain(settings)

    # --- Wire up modules ---
    # Placeholder notify_fn; will be replaced by slack_handler
    async def _noop_notify(text: str, thread_ts: str | None = None):
        logger.info("Notification (no Slack yet): %s", text[:100])

    email_triage = EmailTriage(gmail, brain, _noop_notify)
    calendar_mgr = CalendarManager(calendar, brain)
    reporter = Reporter(calendar_mgr, email_triage, brain)

    # --- Slack app ---
    app = create_slack_app(settings, brain, email_triage, calendar_mgr, reporter)

    # Create the notify function wired to Slack
    from slack_sdk.web.async_client import AsyncWebClient

    slack_client = AsyncWebClient(token=settings.slack_bot_token)
    channel = settings.slack_channel_egao

    async def notify_fn(text: str, thread_ts: str | None = None):
        try:
            await slack_client.chat_postMessage(
                channel=channel,
                text=text,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Failed to send Slack notification")

    # Wire notify into triage and scheduler
    email_triage._notify = notify_fn

    # --- Scheduler ---
    scheduler = setup_scheduler(
        settings, email_triage, calendar_mgr, reporter, notify_fn
    )
    scheduler.start()
    logger.info("Scheduler started")

    # --- Startup notification ---
    await notify_fn("秘書くん起動したよ〜！💪 メールもカレンダーも任せてね")

    # --- Start Slack (blocks) ---
    logger.info("Starting Slack Socket Mode...")
    await start_slack_app(app, settings)


def run():
    """Entry point for pyproject.toml script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("秘書くん shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    run()
