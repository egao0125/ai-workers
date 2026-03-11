"""セールスくん - APScheduler setup for periodic tasks.

Ports vercel cron jobs to APScheduler (same pattern as hisho):
  - Bottleneck check: daily 09:15 JST
  - KPI report: daily 09:30 JST
  - Gmail check: every 5 minutes
  - Weekly report: Monday 09:00 JST
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agents.sales.config import Settings
from agents.sales.gmail_client import GmailClient
from agents.sales.pipeline import Pipeline

logger = logging.getLogger(__name__)

NotifyFn = Callable[[str, str | None], Awaitable[None]]
SlackPostFn = Callable[[str, str], Awaitable[str | None]]


def setup_scheduler(
    settings: Settings,
    pipeline: Pipeline,
    gmail: GmailClient | None,
    slack_post_fn: SlackPostFn | None,
    notify_fn: NotifyFn | None = None,
) -> AsyncIOScheduler:
    """Configure and return the scheduler (not started yet)."""
    scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")

    # --- Job: Check Gmail every 5 minutes ---
    if gmail:
        async def job_check_gmail():
            logger.info("Scheduled: checking Gmail")
            try:
                emails = gmail.list_new_emails(max_results=10)
                watch_email = settings.gmail_watch_email
                allowed_senders = settings.allowed_sender_list

                for email in emails:
                    # Skip self-sent
                    if email.from_email == watch_email:
                        continue

                    # Only process allowed senders (e.g., Framer forms)
                    if allowed_senders and email.from_email not in allowed_senders:
                        logger.info(
                            "Skipping non-inquiry email from %s", email.from_email
                        )
                        continue

                    await pipeline.process_email_intake(email, slack_post_fn)

            except Exception:
                logger.exception("Gmail check job failed")

        scheduler.add_job(
            job_check_gmail,
            IntervalTrigger(minutes=5),
            id="gmail_check",
            name="Gmail check",
            misfire_grace_time=60,
        )

    # --- Job: Bottleneck check daily at 09:15 JST ---
    async def job_bottleneck_check():
        logger.info("Scheduled: bottleneck check")
        try:
            await pipeline.check_bottlenecks(slack_post_fn)
        except Exception:
            logger.exception("Bottleneck check job failed")

    scheduler.add_job(
        job_bottleneck_check,
        CronTrigger(hour=9, minute=15, day_of_week="mon-fri"),
        id="bottleneck_check",
        name="Bottleneck check (09:15 JST)",
        misfire_grace_time=300,
    )

    # --- Job: KPI report daily at 09:30 JST ---
    async def job_kpi_report():
        logger.info("Scheduled: KPI report")
        try:
            await pipeline.post_kpi_report(slack_post_fn)
        except Exception:
            logger.exception("KPI report job failed")

    scheduler.add_job(
        job_kpi_report,
        CronTrigger(hour=9, minute=30, day_of_week="mon-fri"),
        id="kpi_report",
        name="KPI report (09:30 JST)",
        misfire_grace_time=300,
    )

    # --- Job: Weekly report Monday 09:00 JST ---
    async def job_weekly_report():
        logger.info("Scheduled: weekly report")
        try:
            kpi_result = pipeline.get_kpi_summary("weekly")
            if not kpi_result.get("success"):
                return

            kpi = kpi_result["data"]
            text = (
                f"📊 週次レポート\n\n"
                f"*今週の流入:* {kpi['weekly_count']}件\n"
                f"*先週:* {kpi['previous_week_count']}件 "
                f"({'+' if kpi['week_over_week_change'] >= 0 else ''}"
                f"{kpi['week_over_week_change']}%)\n"
                f"*MTD:* {kpi['mtd_inquiries']}件 / "
                f"月目標{kpi['monthly_target']}件 "
                f"(進捗率 {kpi['progress_rate']}%)\n"
                f"*72h超え未返信:* {kpi['unreplied_72h']}件\n"
                f"*平均返信時間:* {kpi['avg_reply_time_hours']}時間"
            )

            if slack_post_fn and not settings.shadow_mode:
                await slack_post_fn(settings.cs_channel_id, text)
            else:
                logger.info("SHADOW_MODE: Would post weekly report")

        except Exception:
            logger.exception("Weekly report job failed")

    scheduler.add_job(
        job_weekly_report,
        CronTrigger(hour=9, minute=0, day_of_week="mon"),
        id="weekly_report",
        name="Weekly report (Monday 09:00 JST)",
        misfire_grace_time=300,
    )

    logger.info(
        "Scheduler configured: Gmail every 5m, bottleneck 09:15, "
        "KPI 09:30, weekly Monday 09:00"
    )

    return scheduler
