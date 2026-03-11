"""参謀くん - Entry point.

Wires all modules together and starts the bot.

Usage:
    python -m agents.sanbou.main
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agents.sanbou.brain import Brain
from agents.sanbou.config import get_settings
from agents.sanbou.db import Database
from agents.sanbou.reporter import Reporter
from agents.sanbou.scheduler import setup_scheduler
from agents.sanbou.slack_handler import create_slack_app, start_slack_app
from agents.sanbou.team_monitor import TeamMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sanbou")


async def main() -> None:
    logger.info("参謀くん starting up...")
    settings = get_settings()

    # --- Initialize core ---
    db = Database(db_path=settings.sanbou_db_path)
    brain = Brain(settings, db)

    team_monitor = TeamMonitor(
        db,
        own_bot_id=settings.sanbou_bot_id,
        monitored_channels=settings.monitored_channel_set,
        silent_channels=settings.silent_channel_set,
    )

    reporter = Reporter(brain, db)

    # --- Slack app ---
    app = create_slack_app(settings, brain, db, team_monitor, reporter)

    # --- Notify function wired to Slack ---
    from slack_sdk.web.async_client import AsyncWebClient

    slack_client = AsyncWebClient(token=settings.sanbou_slack_bot_token)
    pulse_channel = settings.sanbou_pulse_channel

    async def notify_fn(text: str, thread_ts: str | None = None):
        if settings.sanbou_shadow_mode:
            logger.info("Shadow mode notification: %s", text[:100])
            return
        try:
            channel = pulse_channel
            if channel:
                await slack_client.chat_postMessage(
                    channel=channel,
                    text=text,
                    thread_ts=thread_ts,
                )
        except Exception:
            logger.exception("Failed to send Slack notification")

    # --- Scheduler ---
    scheduler = setup_scheduler(
        settings, brain, db, team_monitor, reporter, notify_fn
    )
    scheduler.start()
    logger.info("Scheduler started")

    # --- Startup notification ---
    await notify_fn("参謀くん起動。全チャンネル監視開始。")

    # --- Start Slack (blocks) ---
    logger.info("Starting Slack Socket Mode...")
    await start_slack_app(app, settings)


def run():
    """Entry point for pyproject.toml script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("参謀くん shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    run()
