"""参謀くん - Message ingestion & team monitoring.

Ported from kashikabot's ingest.ts + profile-builder.ts + contribution.ts
+ praise-detector.ts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agents.sanbou.db import Database

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Praise detection patterns (from praise-detector.ts)
# ------------------------------------------------------------------

PRAISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ナイス|nice", re.IGNORECASE),
    re.compile(r"すごい|凄い|すげー"),
    re.compile(r"ありがとう|あざす|thanks", re.IGNORECASE),
    re.compile(r"助かった|助かる|助かります"),
    re.compile(r"神|最高|天才"),
    re.compile(r"グッジョブ|good\s*job", re.IGNORECASE),
    re.compile(r"お疲れ様|おつかれ"),
    re.compile(r"マージした|リリースした|デプロイした|公開した"),
    re.compile(r"達成|突破|完了"),
    re.compile(r"\d+件.*達成|\d+%.*改善|\d+%.*増"),
]


def detect_praise(text: str) -> str | None:
    """Check if a message contains praise or achievement signals."""
    for pattern in PRAISE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(0)
    return None


def extract_praised_user(text: str) -> str | None:
    """Extract the user being praised from @mentions."""
    m = re.search(r"<@(U[A-Z0-9]+)>", text)
    return m.group(1) if m else None


# ------------------------------------------------------------------
# Report detection
# ------------------------------------------------------------------

_GYOUMU_REPORT_RE = re.compile(r"【報告者[：:]")


def is_gyoumu_report(text: str) -> bool:
    """Detect 業務報告君 messages by content pattern."""
    return bool(_GYOUMU_REPORT_RE.search(text))


# ------------------------------------------------------------------
# Contribution aggregation (from contribution.ts)
# ------------------------------------------------------------------


def aggregate_user_stats(
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate raw messages into daily stats for a user."""
    channels: set[str] = set()
    channel_counts: dict[str, int] = {}
    total_words = 0
    thread_count = 0

    for msg in messages:
        ch = msg["channel_id"]
        channels.add(ch)
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
        total_words += msg.get("word_count", 0)
        if msg.get("thread_ts"):
            thread_count += 1

    top_channels = sorted(
        channel_counts.keys(), key=lambda c: channel_counts[c], reverse=True
    )[:5]

    return {
        "message_count": len(messages),
        "thread_count": thread_count,
        "channels_active": len(channels),
        "avg_word_count": total_words / len(messages) if messages else 0,
        "top_channels": top_channels,
    }


# ------------------------------------------------------------------
# Message Ingestion
# ------------------------------------------------------------------


class TeamMonitor:
    """Handles message ingestion, report detection, and profile building."""

    def __init__(
        self,
        db: Database,
        *,
        own_bot_id: str = "",
        monitored_channels: set[str] | None = None,
        silent_channels: set[str] | None = None,
    ) -> None:
        self._db = db
        self._own_bot_id = own_bot_id
        self._monitored_channels = monitored_channels or set()
        self._silent_channels = silent_channels or set()

    def should_monitor(self, channel_id: str) -> bool:
        """Check if a channel should be monitored."""
        if not self._monitored_channels:
            return True  # empty = all channels
        return channel_id in self._monitored_channels

    def is_silent(self, channel_id: str) -> bool:
        """Check if a channel is in silent mode (ingest only, no reply)."""
        return channel_id in self._silent_channels

    def should_respond(self, text: str, bot_id: str | None = None) -> bool:
        """Check if the bot should respond to this message
        (mentioned by name or pattern).
        """
        if bot_id and bot_id == self._own_bot_id:
            return False
        return bool(
            re.search(
                r"参謀くん|さんぼうくん|sanbou-kun|sanboukun",
                text,
                re.IGNORECASE,
            )
        )

    def ingest_message(
        self,
        *,
        slack_ts: str,
        thread_ts: str | None,
        user_id: str,
        channel_id: str,
        text: str,
        has_files: bool = False,
    ) -> None:
        """Capture a Slack message to the database."""
        # Calculate metadata
        word_count = len(text.split()) or len(text)
        has_code = "```" in text
        has_link = bool(re.search(r"https?://", text))

        try:
            self._db.insert_message(
                slack_ts=slack_ts,
                thread_ts=thread_ts,
                user_id=user_id,
                channel_id=channel_id,
                text=text,
                word_count=word_count,
                has_code=has_code,
                has_link=has_link,
                has_file=has_files,
            )
        except Exception:
            logger.warning("Message insert failed ts=%s", slack_ts, exc_info=True)

        # Check for praise
        praise_match = detect_praise(text)
        if praise_match:
            praised_user = extract_praised_user(text)
            if praised_user:
                try:
                    self._db.insert_praise(
                        user_id=praised_user,
                        description=praise_match,
                        source_ts=slack_ts,
                        channel_id=channel_id,
                    )
                    logger.info(
                        "Praise detected: %s -> %s (%s)",
                        user_id,
                        praised_user,
                        praise_match,
                    )
                except Exception:
                    logger.warning("Praise insert failed", exc_info=True)

    async def aggregate_daily(self, brain) -> dict[str, Any]:
        """Aggregate yesterday's stats per user. Returns summary dict."""
        from datetime import datetime, timedelta, timezone

        jst = timezone(timedelta(hours=9))
        yesterday = (datetime.now(jst) - timedelta(days=1)).strftime("%Y-%m-%d")

        messages = self._db.get_messages_by_date(yesterday)
        if not messages:
            logger.info("No messages to aggregate for %s", yesterday)
            return {"date": yesterday, "users": 0, "messages": 0}

        # Group by user
        by_user: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            by_user.setdefault(msg["user_id"], []).append(msg)

        for user_id, user_msgs in by_user.items():
            stats = aggregate_user_stats(user_msgs)

            # Sentiment analysis via brain (Haiku)
            sentiment_score = await brain.analyze_sentiment(
                [m["text"] for m in user_msgs[:20]]
            )

            self._db.upsert_daily_stat(
                user_id=user_id,
                date=yesterday,
                message_count=stats["message_count"],
                thread_count=stats["thread_count"],
                channels_active=stats["channels_active"],
                avg_word_count=stats["avg_word_count"],
                sentiment_score=sentiment_score,
                top_channels=json.dumps(stats["top_channels"]),
                top_topics=None,
            )

        logger.info(
            "Daily stats aggregated: date=%s users=%d messages=%d",
            yesterday,
            len(by_user),
            len(messages),
        )
        return {
            "date": yesterday,
            "users": len(by_user),
            "messages": len(messages),
        }

    async def update_profiles(self, brain) -> int:
        """Rebuild profiles from recent activity for all users."""
        users = self._db.get_distinct_users()
        updated = 0

        for user_id in users:
            try:
                recent_msgs = self._db.get_recent_messages(user_id, 100)
                week_stats = self._db.get_daily_stats(user_id, 7)
                existing = self._db.get_profile(user_id)

                if not recent_msgs:
                    continue

                display_name = (existing or {}).get("display_name") or user_id
                profile_data = await brain.build_profile(
                    display_name, recent_msgs, week_stats, existing
                )

                self._db.upsert_profile(
                    user_id=user_id,
                    display_name=display_name,
                    **profile_data,
                )
                updated += 1
                logger.info("Profile updated: %s (%s)", user_id, display_name)
            except Exception:
                logger.exception("Failed to update profile: %s", user_id)

        return updated
