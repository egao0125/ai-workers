"""参謀くん - SQLite database layer.

Ported from kashikabot's Vercel Postgres schema to local SQLite.
Auto-creates tables on init.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


class Database:
    """SQLite-backed storage for 参謀くん."""

    def __init__(self, db_path: str = "sanbou.db") -> None:
        self._db_path = db_path
        self._ensure_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slack_ts TEXT NOT NULL,
                    thread_ts TEXT,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    word_count INTEGER NOT NULL DEFAULT 0,
                    has_code INTEGER NOT NULL DEFAULT 0,
                    has_link INTEGER NOT NULL DEFAULT 0,
                    has_file INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(slack_ts, channel_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_user_date
                    ON messages(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_channel_date
                    ON messages(channel_id, created_at);

                CREATE TABLE IF NOT EXISTS member_profiles (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT,
                    role TEXT,
                    base_info TEXT,
                    work_style TEXT,
                    strengths TEXT,
                    communication_style TEXT,
                    recent_contributions TEXT,
                    growth_signals TEXT,
                    energy_indicator TEXT,
                    personality TEXT,
                    skills TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS daily_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    thread_count INTEGER NOT NULL DEFAULT 0,
                    channels_active INTEGER NOT NULL DEFAULT 0,
                    avg_word_count REAL NOT NULL DEFAULT 0,
                    sentiment_score REAL,
                    top_channels TEXT,
                    top_topics TEXT,
                    UNIQUE(user_id, date)
                );

                CREATE INDEX IF NOT EXISTS idx_daily_stats_user_date
                    ON daily_stats(user_id, date);

                CREATE TABLE IF NOT EXISTS weekly_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week_start TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    member_highlights TEXT,
                    team_wins TEXT,
                    blockers TEXT,
                    posted_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(week_start)
                );

                CREATE TABLE IF NOT EXISTS praise_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    source_ts TEXT,
                    channel_id TEXT,
                    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE(category, key)
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_time TEXT NOT NULL,
                    schedule_day_of_week INTEGER,
                    schedule_date TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_fired_at TEXT
                );
                """
            )
        logger.info("Database schema ensured: %s", self._db_path)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def insert_message(
        self,
        *,
        slack_ts: str,
        thread_ts: str | None,
        user_id: str,
        channel_id: str,
        text: str,
        word_count: int,
        has_code: bool,
        has_link: bool,
        has_file: bool,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO messages
                    (slack_ts, thread_ts, user_id, channel_id, text,
                     word_count, has_code, has_link, has_file, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    slack_ts,
                    thread_ts,
                    user_id,
                    channel_id,
                    text[:5000],
                    word_count,
                    int(has_code),
                    int(has_link),
                    int(has_file),
                ),
            )

    def get_messages_by_date(self, date_str: str) -> list[dict[str, Any]]:
        """Get all messages for a given date (YYYY-MM-DD)."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, channel_id, text, thread_ts,
                       word_count, slack_ts
                FROM messages
                WHERE date(created_at) = ?
                ORDER BY created_at
                """,
                (date_str,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent_messages(
        self, user_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT text, channel_id, created_at
                FROM messages
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_distinct_users(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT user_id FROM messages ORDER BY user_id"
            ).fetchall()
        return [r["user_id"] for r in rows]

    def get_message_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM messages").fetchone()
        return row["cnt"] if row else 0

    def cleanup_old_messages(self, retention_days: int = 30) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE created_at < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
        return cur.rowcount

    def cleanup_old_praise(self, retention_days: int = 90) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM praise_events WHERE detected_at < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Daily Stats
    # ------------------------------------------------------------------

    def upsert_daily_stat(
        self,
        *,
        user_id: str,
        date: str,
        message_count: int,
        thread_count: int,
        channels_active: int,
        avg_word_count: float,
        sentiment_score: float | None,
        top_channels: str | None,
        top_topics: str | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_stats
                    (user_id, date, message_count, thread_count,
                     channels_active, avg_word_count, sentiment_score,
                     top_channels, top_topics)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, date) DO UPDATE SET
                    message_count = excluded.message_count,
                    thread_count = excluded.thread_count,
                    channels_active = excluded.channels_active,
                    avg_word_count = excluded.avg_word_count,
                    sentiment_score = excluded.sentiment_score,
                    top_channels = excluded.top_channels,
                    top_topics = excluded.top_topics
                """,
                (
                    user_id,
                    date,
                    message_count,
                    thread_count,
                    channels_active,
                    avg_word_count,
                    sentiment_score,
                    top_channels,
                    top_topics,
                ),
            )

    def get_daily_stats(self, user_id: str, days: int = 7) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_stats
                WHERE user_id = ?
                  AND date >= date('now', ?)
                ORDER BY date DESC
                """,
                (user_id, f"-{days} days"),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_weekly_stats(
        self, week_start: str, week_end: str
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_stats
                WHERE date >= ? AND date < ?
                ORDER BY user_id, date
                """,
                (week_start, week_end),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Member Profiles
    # ------------------------------------------------------------------

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM member_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_all_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM member_profiles ORDER BY display_name"
            ).fetchall()
        return [dict(r) for r in rows]

    def upsert_profile(
        self,
        *,
        user_id: str,
        display_name: str,
        role: str | None = None,
        base_info: str | None = None,
        work_style: str = "データ不足",
        strengths: str = "データ不足",
        communication_style: str = "データ不足",
        recent_contributions: str = "データ不足",
        growth_signals: str = "データ不足",
        energy_indicator: str = "🟡 Neutral",
        personality: str | None = None,
        skills: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO member_profiles
                    (user_id, display_name, role, base_info,
                     work_style, strengths, communication_style,
                     recent_contributions, growth_signals, energy_indicator,
                     personality, skills, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    role = COALESCE(excluded.role, member_profiles.role),
                    base_info = COALESCE(excluded.base_info, member_profiles.base_info),
                    work_style = excluded.work_style,
                    strengths = excluded.strengths,
                    communication_style = excluded.communication_style,
                    recent_contributions = excluded.recent_contributions,
                    growth_signals = excluded.growth_signals,
                    energy_indicator = excluded.energy_indicator,
                    personality = COALESCE(excluded.personality, member_profiles.personality),
                    skills = COALESCE(excluded.skills, member_profiles.skills),
                    updated_at = datetime('now')
                """,
                (
                    user_id,
                    display_name,
                    role,
                    base_info,
                    work_style,
                    strengths,
                    communication_style,
                    recent_contributions,
                    growth_signals,
                    energy_indicator,
                    personality,
                    skills,
                ),
            )

    # ------------------------------------------------------------------
    # Weekly Summaries
    # ------------------------------------------------------------------

    def insert_weekly_summary(
        self,
        *,
        week_start: str,
        summary: str,
        member_highlights: str,
        team_wins: str,
        blockers: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO weekly_summaries
                    (week_start, summary, member_highlights, team_wins, blockers)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(week_start) DO UPDATE SET
                    summary = excluded.summary,
                    member_highlights = excluded.member_highlights,
                    team_wins = excluded.team_wins,
                    blockers = excluded.blockers,
                    posted_at = datetime('now')
                """,
                (week_start, summary, member_highlights, team_wins, blockers),
            )

    # ------------------------------------------------------------------
    # Praise Events
    # ------------------------------------------------------------------

    def insert_praise(
        self,
        *,
        user_id: str,
        description: str,
        source_ts: str,
        channel_id: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO praise_events
                    (user_id, description, source_ts, channel_id)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, description, source_ts, channel_id),
            )

    def get_recent_praise(self, days: int = 7) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT user_id, description, detected_at
                FROM praise_events
                WHERE detected_at >= datetime('now', ?)
                ORDER BY detected_at DESC
                """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    def upsert_memory(self, category: str, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory (category, key, value, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(category, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = datetime('now')
                """,
                (category, key, value),
            )

    def get_memories(self, category: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if category:
                rows = conn.execute(
                    """
                    SELECT category, key, value
                    FROM memory
                    WHERE category = ?
                    ORDER BY updated_at DESC
                    """,
                    (category,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT category, key, value
                    FROM memory
                    ORDER BY category, updated_at DESC
                    """
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Activity Stats (aggregated from raw messages)
    # ------------------------------------------------------------------

    def get_user_activity_stats(self, days: int = 7) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    user_id,
                    COUNT(*) AS message_count,
                    COUNT(DISTINCT thread_ts) AS thread_count,
                    COUNT(DISTINCT channel_id) AS channels_active,
                    ROUND(AVG(word_count), 1) AS avg_word_count
                FROM messages
                WHERE created_at >= datetime('now', ?)
                GROUP BY user_id
                ORDER BY message_count DESC
                """,
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    def save_reminder(
        self,
        *,
        reminder_id: str,
        user_id: str,
        channel_id: str,
        text: str,
        schedule_type: str,
        schedule_time: str,
        schedule_day_of_week: int | None = None,
        schedule_date: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reminders
                    (id, user_id, channel_id, text,
                     schedule_type, schedule_time,
                     schedule_day_of_week, schedule_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reminder_id,
                    user_id,
                    channel_id,
                    text,
                    schedule_type,
                    schedule_time,
                    schedule_day_of_week,
                    schedule_date,
                ),
            )

    def list_reminders(self, user_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if user_id:
                rows = conn.execute(
                    """
                    SELECT * FROM reminders
                    WHERE enabled = 1 AND user_id = ?
                    ORDER BY created_at
                    """,
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reminders
                    WHERE enabled = 1
                    ORDER BY created_at
                    """
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id = ?", (reminder_id,)
            )
        return cur.rowcount > 0

    def mark_reminder_fired(self, reminder_id: str) -> None:
        with self._connect() as conn:
            # Check if one-time
            row = conn.execute(
                "SELECT schedule_type FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            if not row:
                return
            if row["schedule_type"] == "once":
                conn.execute(
                    "DELETE FROM reminders WHERE id = ?", (reminder_id,)
                )
            else:
                conn.execute(
                    "UPDATE reminders SET last_fired_at = datetime('now') WHERE id = ?",
                    (reminder_id,),
                )

    def get_due_reminders(self) -> list[dict[str, Any]]:
        """Get reminders that should fire now (JST-aware)."""
        now_jst = datetime.now(JST)
        current_day = now_jst.weekday()  # 0=Mon in Python
        # Convert to JS-style: 0=Sun, 1=Mon, ..., 6=Sat
        current_day_js = (current_day + 1) % 7
        current_date = now_jst.strftime("%Y-%m-%d")

        all_reminders = self.list_reminders()
        due: list[dict[str, Any]] = []

        for r in all_reminders:
            if not r.get("enabled"):
                continue

            # Skip if already fired today
            last_fired = r.get("last_fired_at")
            if last_fired:
                last_fired_date = last_fired[:10]
                if last_fired_date == current_date:
                    continue

            stype = r["schedule_type"]
            if stype == "daily":
                due.append(r)
            elif stype == "weekly":
                if r.get("schedule_day_of_week") == current_day_js:
                    due.append(r)
            elif stype == "once":
                if r.get("schedule_date") == current_date:
                    due.append(r)

        return due
