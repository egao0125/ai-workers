"""参謀くん - APScheduler setup for periodic tasks.

Ported from kashikabot's vercel.json cron config:
  - Daily aggregate: 16:30 JST (was "30 16 * * *")
  - Daily report: 00:00 Tue-Fri (was "0 0 * * 1-5")
  - Weekly report: 00:00 Monday (handled by daily_report detecting Monday)
  - Reminder delivery: check every 5 min (was "0 0 * * *" for reminders)
  - DB cleanup: Mondays (30-day messages, 90-day praise)
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.sanbou.brain import Brain
from agents.sanbou.config import Settings
from agents.sanbou.db import Database
from agents.sanbou.reporter import Reporter
from agents.sanbou.team_monitor import TeamMonitor

logger = logging.getLogger(__name__)


def setup_scheduler(
    settings: Settings,
    brain: Brain,
    db: Database,
    team_monitor: TeamMonitor,
    reporter: Reporter,
    notify_fn: Callable[[str, str | None], Awaitable[None]],
) -> AsyncIOScheduler:
    """Configure and return the scheduler (not started yet)."""
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
    shadow_mode = settings.sanbou_shadow_mode

    # ------------------------------------------------------------------
    # Daily Aggregate: 16:30 JST every day
    # Aggregates yesterday's messages into daily_stats per user,
    # runs sentiment analysis, and rebuilds member profiles.
    # ------------------------------------------------------------------

    async def job_daily_aggregate():
        logger.info("Scheduled: daily aggregation")
        try:
            result = await team_monitor.aggregate_daily(brain)
            logger.info("Daily aggregation result: %s", result)

            # Update profiles after aggregation
            updated = await team_monitor.update_profiles(brain)
            logger.info("Profiles updated: %d", updated)
        except Exception:
            logger.exception("Daily aggregation job failed")

    scheduler.add_job(
        job_daily_aggregate,
        CronTrigger(hour=16, minute=30),
        id="daily_aggregate",
        name="Daily aggregate (16:30 JST)",
        misfire_grace_time=300,
    )

    # ------------------------------------------------------------------
    # Daily/Weekly Report: 00:00 JST Tue-Sat (covering Mon-Fri)
    # On Tuesday (covering Monday), this produces a weekly report.
    # Other days produce daily reports.
    # Note: vercel cron was "0 0 * * 1-5" (Mon-Fri midnight),
    # which reports on the previous day. Here we use Tue-Sat to
    # cover Mon-Fri activity.
    # Actually kashikabot runs Mon-Fri at midnight to report on
    # the previous day, with Monday producing a weekly report.
    # ------------------------------------------------------------------

    async def job_daily_report():
        logger.info("Scheduled: daily/weekly report")
        try:
            result = await reporter.generate_daily_report()
            report_type = result.get("type", "daily")
            header = result.get("header", "")
            blocks = result.get("blocks", [])
            summary = result.get("summary", "")

            if not summary or summary == "データなし":
                logger.info("No data for report, skipping post")
                return

            pulse_channel = settings.sanbou_pulse_channel
            if not pulse_channel:
                logger.warning("SANBOU_PULSE_CHANNEL not set, skipping report post")
                return

            if shadow_mode:
                logger.info(
                    "Shadow mode: would post %s report to %s: %s",
                    report_type,
                    pulse_channel,
                    summary[:100],
                )
            else:
                from slack_sdk.web.async_client import AsyncWebClient

                slack = AsyncWebClient(token=settings.sanbou_slack_bot_token)
                await slack.chat_postMessage(
                    channel=pulse_channel,
                    text=header,
                    blocks=blocks,
                )
                logger.info(
                    "%s report posted to %s", report_type, pulse_channel
                )

            # DB cleanup on weekly reports (Mondays)
            if report_type == "weekly":
                deleted_msgs = db.cleanup_old_messages(30)
                deleted_praise = db.cleanup_old_praise(90)
                remaining = db.get_message_count()
                logger.info(
                    "Weekly cleanup: deleted_msgs=%d deleted_praise=%d remaining=%d",
                    deleted_msgs,
                    deleted_praise,
                    remaining,
                )
        except Exception:
            logger.exception("Daily report job failed")

    scheduler.add_job(
        job_daily_report,
        CronTrigger(hour=0, minute=0, day_of_week="mon-fri"),
        id="daily_report",
        name="Daily/Weekly report (00:00 JST Mon-Fri)",
        misfire_grace_time=600,
    )

    # ------------------------------------------------------------------
    # Reminder check: every 5 minutes
    # ------------------------------------------------------------------

    async def job_check_reminders():
        try:
            from datetime import datetime, timedelta, timezone

            jst = timezone(timedelta(hours=9))
            now_jst = datetime.now(jst)
            current_time = now_jst.strftime("%H:%M")

            due_reminders = db.get_due_reminders()
            for r in due_reminders:
                # Only fire if current time matches schedule_time (HH:MM)
                if r["schedule_time"] != current_time:
                    continue

                reminder_text = f"🔔 リマインダー: {r['text']}"
                if shadow_mode:
                    logger.info(
                        "Shadow mode: would send reminder to %s: %s",
                        r["channel_id"],
                        r["text"],
                    )
                else:
                    try:
                        from slack_sdk.web.async_client import AsyncWebClient

                        slack = AsyncWebClient(
                            token=settings.sanbou_slack_bot_token
                        )
                        await slack.chat_postMessage(
                            channel=r["channel_id"],
                            text=reminder_text,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send reminder %s", r["id"]
                        )
                        continue

                db.mark_reminder_fired(r["id"])
                logger.info("Reminder fired: %s", r["id"])
        except Exception:
            logger.exception("Reminder check job failed")

    scheduler.add_job(
        job_check_reminders,
        IntervalTrigger(minutes=5),
        id="reminder_check",
        name="Reminder check (every 5min)",
        misfire_grace_time=60,
    )

    logger.info(
        "Scheduler configured: "
        "daily_aggregate=16:30, daily_report=00:00 Mon-Fri, "
        "reminders=every 5min, shadow_mode=%s",
        shadow_mode,
    )

    return scheduler
