"""秘書くん - Google Calendar API client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

from agents.hisho.config import Settings

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Protected slots: these events are NEVER moved or double-booked
PROTECTED_KEYWORDS = [
    "スタンドアップ",
    "standup",
    "中国語",
    "AH395",
    "美術史",
]


@dataclass
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    attendees: list[str] | None = None
    is_protected: bool = False


@dataclass
class TimeSlot:
    start: datetime
    end: datetime


class CalendarClient:
    """Google Calendar API wrapper."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tz = pytz.timezone(settings.google_calendar_timezone)
        self._service = None

    @property
    def service(self):
        if self._service is None:
            creds = service_account.Credentials.from_service_account_file(
                self._settings.google_service_account_key_path,
                scopes=SCOPES,
            )
            self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def get_events(
        self, date: str | None = None, days: int = 1
    ) -> list[CalendarEvent]:
        """Get events for a date range. date format: YYYY-MM-DD."""
        if date:
            start_dt = self._tz.localize(datetime.strptime(date, "%Y-%m-%d"))
        else:
            start_dt = datetime.now(self._tz).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        end_dt = start_dt + timedelta(days=days)

        try:
            result = (
                self.service.events()
                .list(
                    calendarId=self._settings.google_calendar_id,
                    timeMin=start_dt.isoformat(),
                    timeMax=end_dt.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch calendar events")
            return []

        events = []
        for item in result.get("items", []):
            event = self._parse_event(item)
            if event:
                events.append(event)
        return events

    def get_today_events(self) -> list[CalendarEvent]:
        """Get today's events."""
        today = datetime.now(self._tz).strftime("%Y-%m-%d")
        return self.get_events(today)

    def find_free_slots(
        self,
        date: str,
        duration_minutes: int = 30,
        start_hour: int = 10,
        end_hour: int = 19,
    ) -> list[TimeSlot]:
        """Find free time slots on a given date."""
        events = self.get_events(date)
        day_start = self._tz.localize(
            datetime.strptime(f"{date} {start_hour:02d}:00", "%Y-%m-%d %H:%M")
        )
        day_end = self._tz.localize(
            datetime.strptime(f"{date} {end_hour:02d}:00", "%Y-%m-%d %H:%M")
        )

        busy = [(e.start, e.end) for e in events]
        busy.sort(key=lambda x: x[0])

        free = []
        cursor = day_start
        for busy_start, busy_end in busy:
            if cursor + timedelta(minutes=duration_minutes) <= busy_start:
                free.append(TimeSlot(start=cursor, end=busy_start))
            cursor = max(cursor, busy_end)

        if cursor + timedelta(minutes=duration_minutes) <= day_end:
            free.append(TimeSlot(start=cursor, end=day_end))

        return free

    def create_event(
        self,
        summary: str,
        start: datetime,
        end: datetime,
        description: str = "",
        attendees: list[str] | None = None,
    ) -> CalendarEvent | None:
        """Create a calendar event. Checks for conflicts and protected slots."""
        # Check protected slots
        if self._conflicts_with_protected(start, end):
            logger.warning(
                "Cannot create event: conflicts with protected slot (%s - %s)",
                start, end,
            )
            return None

        event_body: dict = {
            "summary": summary,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": self._settings.google_calendar_timezone,
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": self._settings.google_calendar_timezone,
            },
        }
        if description:
            event_body["description"] = description
        if attendees:
            event_body["attendees"] = [{"email": a} for a in attendees]

        try:
            result = (
                self.service.events()
                .insert(
                    calendarId=self._settings.google_calendar_id,
                    body=event_body,
                )
                .execute()
            )
            logger.info("Event created: %s", result.get("id"))
            return self._parse_event(result)
        except Exception:
            logger.exception("Failed to create event")
            return None

    def detect_conflicts(
        self, start: datetime, end: datetime
    ) -> list[CalendarEvent]:
        """Find events that overlap with the given time range."""
        date_str = start.strftime("%Y-%m-%d")
        events = self.get_events(date_str)
        return [
            e
            for e in events
            if e.start < end and e.end > start
        ]

    def _conflicts_with_protected(
        self, start: datetime, end: datetime
    ) -> bool:
        """Check if time range overlaps with protected events."""
        conflicts = self.detect_conflicts(start, end)
        return any(c.is_protected for c in conflicts)

    def _parse_event(self, item: dict) -> CalendarEvent | None:
        """Parse a Google Calendar API event item."""
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})

        start_str = start_raw.get("dateTime") or start_raw.get("date")
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        if not start_str or not end_str:
            return None

        try:
            start = datetime.fromisoformat(start_str)
            end = datetime.fromisoformat(end_str)
        except ValueError:
            # All-day events come as YYYY-MM-DD
            start = self._tz.localize(datetime.strptime(start_str, "%Y-%m-%d"))
            end = self._tz.localize(datetime.strptime(end_str, "%Y-%m-%d"))

        summary = item.get("summary", "(no title)")
        is_protected = any(
            kw.lower() in summary.lower() for kw in PROTECTED_KEYWORDS
        )

        attendees = [
            a.get("email", "")
            for a in item.get("attendees", [])
        ]

        return CalendarEvent(
            id=item["id"],
            summary=summary,
            start=start,
            end=end,
            description=item.get("description", ""),
            attendees=attendees or None,
            is_protected=is_protected,
        )
