"""秘書くん - Calendar management logic."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz

from agents.hisho.brain import Brain
from agents.hisho.calendar_client import CalendarClient, CalendarEvent, TimeSlot

logger = logging.getLogger(__name__)


@dataclass
class ScheduleResult:
    success: bool
    message: str
    event: CalendarEvent | None = None


class CalendarManager:
    """High-level calendar operations with conflict detection."""

    def __init__(self, calendar: CalendarClient, brain: Brain) -> None:
        self._calendar = calendar
        self._brain = brain
        self._tz = calendar._tz

    def get_daily_schedule(self, date: str | None = None) -> list[CalendarEvent]:
        """Get today's (or specified date's) schedule."""
        if date:
            return self._calendar.get_events(date)
        return self._calendar.get_today_events()

    def get_schedule_text(self, date: str | None = None) -> str:
        """Get a formatted text representation of the schedule."""
        events = self.get_daily_schedule(date)
        if not events:
            return "今日は予定なし！フリーダムだね 🎉"

        lines = []
        for e in events:
            start = e.start.strftime("%H:%M")
            end = e.end.strftime("%H:%M")
            protected = " 🔒" if e.is_protected else ""
            lines.append(f"• {start}-{end} {e.summary}{protected}")

        return "\n".join(lines)

    def schedule_meeting(
        self,
        request: str,
    ) -> ScheduleResult:
        """Parse a meeting request and schedule it.

        Used when sales-kun or others request: "○○社とデモ設定して"
        """
        # Parse the request with brain
        parsed = self._brain.parse_schedule_request(request)
        company = parsed.get("company", "不明")
        duration = parsed.get("duration_minutes", 30)
        preferred = parsed.get("preferred_dates", [])
        notes = parsed.get("notes", "")

        # If no preferred dates, try next 3 business days
        if not preferred:
            now = datetime.now(self._tz)
            for i in range(1, 8):
                d = now + timedelta(days=i)
                if d.weekday() < 5:  # Mon-Fri
                    preferred.append(d.strftime("%Y-%m-%d"))
                if len(preferred) >= 3:
                    break

        # Find free slots across preferred dates (prefer afternoon)
        for date in preferred:
            # Try afternoon first (14:00-18:00), then morning
            for start_hour, end_hour in [(14, 18), (10, 13)]:
                slots = self._calendar.find_free_slots(
                    date, duration, start_hour, end_hour
                )
                if slots:
                    slot = slots[0]
                    event_start = slot.start
                    event_end = event_start + timedelta(minutes=duration)

                    event = self._calendar.create_event(
                        summary=f"{company} デモ",
                        start=event_start,
                        end=event_end,
                        description=f"セールスくん経由\n{notes}",
                    )

                    if event:
                        return ScheduleResult(
                            success=True,
                            message=(
                                f"📅 デモ設定したよ\n"
                                f"• 相手: {company}\n"
                                f"• 日時: {event.start.strftime('%m/%d %H:%M')}-"
                                f"{event.end.strftime('%H:%M')}\n"
                                f"• 形式: オンライン"
                            ),
                            event=event,
                        )

        return ScheduleResult(
            success=False,
            message=f"ごめん、{company}のデモを入れる空きが見つからなかった 😢 手動で調整してくれる？",
        )

    def check_upcoming(self, minutes: int = 15) -> list[CalendarEvent]:
        """Check for events starting within N minutes."""
        now = datetime.now(self._tz)
        window_end = now + timedelta(minutes=minutes)

        events = self._calendar.get_today_events()
        return [
            e
            for e in events
            if now <= e.start <= window_end
        ]

    def get_events_as_dicts(self, date: str | None = None) -> list[dict]:
        """Get events as serializable dicts for brain consumption."""
        events = self.get_daily_schedule(date)
        return [
            {
                "summary": e.summary,
                "start": e.start.strftime("%H:%M"),
                "end": e.end.strftime("%H:%M"),
                "protected": e.is_protected,
            }
            for e in events
        ]
