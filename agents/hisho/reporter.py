"""秘書くん - Report generation."""

from __future__ import annotations

import logging

from agents.hisho.brain import Brain
from agents.hisho.calendar_manager import CalendarManager
from agents.hisho.email_triage import EmailTriage

logger = logging.getLogger(__name__)


class Reporter:
    """Composes structured reports from multiple data sources."""

    def __init__(
        self,
        calendar_mgr: CalendarManager,
        email_triage: EmailTriage,
        brain: Brain,
    ) -> None:
        self._calendar = calendar_mgr
        self._email = email_triage
        self._brain = brain

    def generate_morning_report(self) -> str:
        """Generate the morning summary report."""
        # Gather data
        events = self._calendar.get_events_as_dicts()
        email_summary = self._email.get_summary()

        # Let brain compose in 秘書くん's voice
        report = self._brain.generate_morning_report(
            events=events,
            email_summary=email_summary,
        )
        return report

    def generate_schedule_report(self, date: str | None = None) -> str:
        """Generate a schedule-only report."""
        schedule_text = self._calendar.get_schedule_text(date)
        return f"📅 *今日のスケジュール*\n\n{schedule_text}"
