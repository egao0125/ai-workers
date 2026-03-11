"""セールスくん - Configuration."""

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

    # Slack (separate bot from hisho/sanbou)
    slack_bot_token: str
    slack_app_token: str
    slack_signing_secret: str

    # Slack channels
    sales_monitored_channels: str = ""  # comma-separated channel IDs
    cs_channel_id: str = ""
    sales_pipeline_channel: str = ""  # #ai-sales-pipeline
    escalation_channel: str = ""  # #ai-escalations

    # Bot identity
    sales_bot_id: str = ""  # セールスくん's own bot ID
    escalation_user_id: str = ""  # えがお's Slack user ID

    # Anthropic
    anthropic_api_key: str
    classifier_model: str = "claude-haiku-4-5-20251001"
    reasoner_model: str = "claude-sonnet-4-6"

    # Gmail (Service Account — NOT OAuth)
    gmail_client_email: str = ""
    gmail_private_key: str = ""
    gmail_watch_email: str = ""  # e.g. contact@stepai.co.jp
    gmail_allowed_senders: str = "noreply@framer.com"  # comma-separated

    # Notion
    notion_api_key: str = ""
    notion_inquiries_db_id: str = ""
    notion_clients_db_id: str = ""

    # Operational
    shadow_mode: bool = True  # SALES_SHADOW_MODE: when True, log but don't post
    classifier_confidence_threshold: float = 0.80
    kpi_monthly_target: int = 30
    slack_workspace_domain: str = "stepai"

    # Brave Search (for company research)
    brave_api_key: str = ""

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent

    @property
    def monitored_channel_set(self) -> set[str]:
        raw = self.sales_monitored_channels.strip()
        if not raw:
            return set()
        return {ch.strip() for ch in raw.split(",") if ch.strip()}

    @property
    def allowed_sender_list(self) -> list[str]:
        raw = self.gmail_allowed_senders.strip()
        if not raw:
            return []
        return [s.strip() for s in raw.split(",") if s.strip()]

    @property
    def data_dir(self) -> Path:
        """Directory for JSON state files."""
        d = self.project_root / "data" / "sales"
        d.mkdir(parents=True, exist_ok=True)
        return d


@lru_cache
def get_settings() -> Settings:
    return Settings()
