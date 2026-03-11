"""秘書くん - APScheduler setup for periodic tasks."""

from __future__ import annotations

import logging
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.hisho.calendar_manager import CalendarManager
from agents.hisho.config import Settings
from agents.hisho.email_triage import EmailTriage
from agents.hisho.reporter import Reporter

logger = logging.getLogger(__name__)


def setup_scheduler(
    settings: Settings,
    email_triage: EmailTriage,
    calendar_mgr: CalendarManager,
    reporter: Reporter,
    notify_fn: Callable[[str, str | None], Awaitable[None]],
) -> AsyncIOScheduler:
    """Configure and return the scheduler (not started yet)."""
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # --- Job: Check emails periodically ---
    async def job_check_emails():
        logger.info("Scheduled: checking emails")
        try:
            await email_triage.check_new_emails()
        except Exception:
            logger.exception("Email check job failed")

    scheduler.add_job(
        job_check_emails,
        IntervalTrigger(minutes=settings.email_check_interval_minutes),
        id="email_check",
        name="Email check",
        misfire_grace_time=60,
    )

    # --- Job: Morning report (weekdays 8:30 JST) ---
    async def job_morning_report():
        logger.info("Scheduled: morning report")
        try:
            report = reporter.generate_morning_report()
            await notify_fn(report, None)
        except Exception:
            logger.exception("Morning report job failed")
            await notify_fn("おはよ〜☀️ 朝の報告の生成でエラーが起きちゃった 😢", None)

    scheduler.add_job(
        job_morning_report,
        CronTrigger(
            hour=settings.morning_report_hour,
            minute=settings.morning_report_minute,
            day_of_week="mon-fri",
        ),
        id="morning_report",
        name="Morning report",
        misfire_grace_time=300,
    )

    # --- Job: Upcoming meeting reminder (every minute) ---
    async def job_meeting_reminder():
        try:
            upcoming = calendar_mgr.check_upcoming(minutes=15)
            for event in upcoming:
                msg = (
                    f"⏰ もうすぐ会議だよ！\n"
                    f"• {event.start.strftime('%H:%M')} {event.summary}\n"
                    f"あと15分！準備してね"
                )
                await notify_fn(msg, None)
        except Exception:
            logger.exception("Meeting reminder job failed")

    scheduler.add_job(
        job_meeting_reminder,
        IntervalTrigger(minutes=5),
        id="meeting_reminder",
        name="Meeting reminder",
        misfire_grace_time=60,
    )

    # --- Job: Stale task check (weekdays 17:00 JST) ---
    async def job_stale_check():
        logger.info("Scheduled: stale task check")
        try:
            summary = email_triage.get_summary()
            red_count = summary.get("red_count", 0)
            if red_count > 0:
                items = summary.get("red", [])
                lines = [f"⚠️ まだ対応してないメールが{red_count}件あるよ：\n"]
                for item in items:
                    lines.append(f"🔴 {item.get('summary', item.get('subject', ''))}")
                await notify_fn("\n".join(lines), None)
        except Exception:
            logger.exception("Stale task check failed")

    scheduler.add_job(
        job_stale_check,
        CronTrigger(hour=17, minute=0, day_of_week="mon-fri"),
        id="stale_check",
        name="Stale task check",
        misfire_grace_time=300,
    )

    logger.info(
        "Scheduler configured: email every %dm, morning at %d:%02d, "
        "meeting reminder every 5m, stale check at 17:00",
        settings.email_check_interval_minutes,
        settings.morning_report_hour,
        settings.morning_report_minute,
    )

    return scheduler
