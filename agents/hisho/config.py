"""秘書くん - Configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str
    slack_channel_egao: str = "C09HCJD9GFM"

    # Gmail (OAuth2)
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"

    # Google Calendar
    google_calendar_id: str = "primary"
    google_service_account_key_path: str = "service_account.json"
    google_calendar_timezone: str = "Asia/Tokyo"

    # Anthropic
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-6"

    # Scheduler
    email_check_interval_minutes: int = 5
    morning_report_hour: int = 8
    morning_report_minute: int = 30

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
