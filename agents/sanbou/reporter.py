"""参謀くん - Report generation.

Ported from kashikabot's daily-report.ts + contribution.ts.
Generates daily (Tue-Fri) and weekly (Monday) team reports.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from agents.sanbou.brain import Brain
from agents.sanbou.db import Database

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


# ------------------------------------------------------------------
# QuickChart URL builder (from charts.ts)
# ------------------------------------------------------------------


def _encode_chart(config: dict, width: int = 600, height: int = 300) -> str:
    return (
        f"https://quickchart.io/chart?"
        f"c={quote(json.dumps(config))}"
        f"&w={width}&h={height}&bkg=white"
    )


def activity_chart(labels: list[str], message_counts: list[int]) -> str:
    return _encode_chart(
        {
            "type": "bar",
            "data": {
                "labels": labels,
                "datasets": [
                    {
                        "label": "Messages",
                        "data": message_counts,
                        "backgroundColor": "rgba(54, 162, 235, 0.7)",
                    }
                ],
            },
            "options": {
                "plugins": {"legend": {"display": False}},
                "scales": {"y": {"beginAtZero": True, "ticks": {"precision": 0}}},
            },
        },
        400,
        200,
    )


def contribution_chart(names: list[str], message_counts: list[int]) -> str:
    colors = [
        "rgba(255, 99, 132, 0.7)",
        "rgba(54, 162, 235, 0.7)",
        "rgba(255, 206, 86, 0.7)",
        "rgba(75, 192, 192, 0.7)",
        "rgba(153, 102, 255, 0.7)",
        "rgba(255, 159, 64, 0.7)",
    ]
    return _encode_chart(
        {
            "type": "doughnut",
            "data": {
                "labels": names,
                "datasets": [
                    {
                        "data": message_counts,
                        "backgroundColor": colors[: len(names)],
                    }
                ],
            },
        },
        400,
        300,
    )


def sentiment_chart(labels: list[str], scores: list[float]) -> str:
    return _encode_chart(
        {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [
                    {
                        "label": "Sentiment",
                        "data": scores,
                        "borderColor": "rgba(75, 192, 192, 1)",
                        "fill": False,
                        "tension": 0.3,
                    }
                ],
            },
            "options": {"scales": {"y": {"min": -1, "max": 1}}},
        },
        500,
        250,
    )


# ------------------------------------------------------------------
# Reporter
# ------------------------------------------------------------------


class Reporter:
    """Generates daily and weekly team reports."""

    def __init__(self, brain: Brain, db: Database) -> None:
        self._brain = brain
        self._db = db

    async def generate_daily_report(self) -> dict[str, Any]:
        """Generate a daily report (Tue-Fri: yesterday's summary).
        On Monday, generates a weekly report (7-day lookback).

        Returns a dict with:
            - type: "daily" | "weekly"
            - header: text for the header
            - blocks: Slack blocks
            - summary: text summary
        """
        now_jst = datetime.now(JST)
        jst_day = now_jst.weekday()  # 0=Mon in Python
        is_monday = jst_day == 0
        lookback_days = 7 if is_monday else 1
        report_type = "weekly" if is_monday else "daily"

        range_end = now_jst.strftime("%Y-%m-%d")
        range_start = (now_jst - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%d"
        )

        logger.info(
            "Report starting: type=%s start=%s end=%s",
            report_type,
            range_start,
            range_end,
        )

        stats = self._db.get_weekly_stats(range_start, range_end)
        users = self._db.get_distinct_users()

        if not stats:
            logger.info("No stats for report: %s", report_type)
            return {
                "type": report_type,
                "header": "",
                "blocks": [],
                "summary": "データなし",
            }

        # Build per-user summaries
        user_summaries: list[dict[str, Any]] = []
        user_messages: dict[str, list[str]] = {}

        for user_id in users:
            user_stats = [s for s in stats if s["user_id"] == user_id]
            total_msgs = sum(s["message_count"] for s in user_stats)
            avg_sentiment = (
                sum(s.get("sentiment_score") or 0 for s in user_stats)
                / len(user_stats)
                if user_stats
                else 0
            )

            if total_msgs > 0:
                user_summaries.append(
                    {
                        "userId": user_id,
                        "messages": total_msgs,
                        "sentiment": avg_sentiment,
                    }
                )

            msgs = self._db.get_recent_messages(
                user_id, 30 if is_monday else 10
            )
            user_messages[user_id] = [m["text"][:150] for m in msgs]

        # Generate summary with Claude
        report = await self._brain.generate_report(
            report_type=report_type,
            start_date=range_start,
            end_date=range_end,
            user_summaries=user_summaries,
            user_messages=user_messages,
        )

        # Save to DB (weekly only)
        if is_monday:
            self._db.insert_weekly_summary(
                week_start=range_start,
                summary=report.get("summary", ""),
                member_highlights=json.dumps(
                    report.get("memberHighlights", []), ensure_ascii=False
                ),
                team_wins=json.dumps(
                    report.get("teamWins", []), ensure_ascii=False
                ),
                blockers=json.dumps(
                    report.get("blockers", []), ensure_ascii=False
                ),
            )

        # Build Slack blocks
        header_text = (
            f"📊 Weekly Report: {range_start}"
            if is_monday
            else f"🌤️ Daily Pulse: {range_start}"
        )

        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": header_text,
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": report.get("summary", "")},
            },
            {"type": "divider"},
        ]

        highlights = report.get("memberHighlights", [])
        if highlights:
            hl_text = "\n".join(
                f"- <@{h['userId']}>: {h['highlight']}" for h in highlights
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Member Highlights*\n{hl_text}",
                    },
                }
            )

        wins = report.get("teamWins", [])
        if wins:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Team Wins*\n"
                        + "\n".join(f"- {w}" for w in wins),
                    },
                }
            )

        blockers = report.get("blockers", [])
        if blockers:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Attention*\n"
                        + "\n".join(f"- {b}" for b in blockers),
                    },
                }
            )

        # Contribution chart (weekly only)
        if is_monday and user_summaries:
            names = [u["userId"] for u in user_summaries]
            msg_counts = [u["messages"] for u in user_summaries]
            chart_url = contribution_chart(names, msg_counts)
            blocks.append(
                {
                    "type": "image",
                    "image_url": chart_url,
                    "alt_text": "Team contribution chart",
                }
            )

        return {
            "type": report_type,
            "header": header_text,
            "blocks": blocks,
            "summary": report.get("summary", ""),
        }

    async def generate_weekly_report(self) -> dict[str, Any]:
        """Explicit weekly report (always 7-day lookback)."""
        now_jst = datetime.now(JST)
        range_end = now_jst.strftime("%Y-%m-%d")
        range_start = (now_jst - timedelta(days=7)).strftime("%Y-%m-%d")

        stats = self._db.get_weekly_stats(range_start, range_end)
        users = self._db.get_distinct_users()

        user_summaries: list[dict[str, Any]] = []
        user_messages: dict[str, list[str]] = {}

        for user_id in users:
            user_stats = [s for s in stats if s["user_id"] == user_id]
            total_msgs = sum(s["message_count"] for s in user_stats)
            avg_sentiment = (
                sum(s.get("sentiment_score") or 0 for s in user_stats)
                / len(user_stats)
                if user_stats
                else 0
            )
            if total_msgs > 0:
                user_summaries.append(
                    {
                        "userId": user_id,
                        "messages": total_msgs,
                        "sentiment": avg_sentiment,
                    }
                )
            msgs = self._db.get_recent_messages(user_id, 30)
            user_messages[user_id] = [m["text"][:150] for m in msgs]

        report = await self._brain.generate_report(
            report_type="weekly",
            start_date=range_start,
            end_date=range_end,
            user_summaries=user_summaries,
            user_messages=user_messages,
        )

        self._db.insert_weekly_summary(
            week_start=range_start,
            summary=report.get("summary", ""),
            member_highlights=json.dumps(
                report.get("memberHighlights", []), ensure_ascii=False
            ),
            team_wins=json.dumps(
                report.get("teamWins", []), ensure_ascii=False
            ),
            blockers=json.dumps(
                report.get("blockers", []), ensure_ascii=False
            ),
        )

        return report

    def format_member_highlights(
        self, user_summaries: list[dict[str, Any]]
    ) -> str:
        """Format per-user contribution summary for Slack."""
        if not user_summaries:
            return "データなし"
        lines = []
        for u in sorted(
            user_summaries, key=lambda x: x["messages"], reverse=True
        ):
            lines.append(
                f"<@{u['userId']}>: {u['messages']} msgs "
                f"(sentiment: {u['sentiment']:.2f})"
            )
        return "\n".join(lines)
