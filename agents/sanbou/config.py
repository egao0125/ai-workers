"""参謀くん - Configuration."""

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

    # Slack (separate bot from hisho)
    sanbou_slack_bot_token: str
    sanbou_slack_app_token: str
    sanbou_slack_signing_secret: str

    # Channels to monitor (comma-separated IDs)
    sanbou_monitored_channels: str = ""
    # Channels where sanbou ingests but does NOT reply
    sanbou_silent_channels: str = ""
    # Channel for team pulse / daily reports
    sanbou_pulse_channel: str = ""
    # Bot's own user ID (to ignore own messages)
    sanbou_bot_id: str = ""

    # Anthropic
    anthropic_api_key: str
    anthropic_model_sonnet: str = "claude-sonnet-4-6"
    anthropic_model_haiku: str = "claude-haiku-4-5-20251001"

    # Database
    sanbou_db_path: str = "sanbou.db"

    # Shadow mode: log actions but don't post to Slack
    sanbou_shadow_mode: bool = False

    @property
    def monitored_channel_set(self) -> set[str]:
        return set(
            ch.strip()
            for ch in self.sanbou_monitored_channels.split(",")
            if ch.strip()
        )

    @property
    def silent_channel_set(self) -> set[str]:
        return set(
            ch.strip()
            for ch in self.sanbou_silent_channels.split(",")
            if ch.strip()
        )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
