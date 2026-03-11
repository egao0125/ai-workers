"""セールスくん - Notion API client for CRM.

Ports notion.ts from maakun. Manages inquiries DB and clients DB.
"""

from __future__ import annotations

import logging
from typing import Any

from agents.sales.config import Settings

logger = logging.getLogger(__name__)

# Temperature mapping: English -> Japanese
TEMP_MAP = {"low": "低", "medium": "中", "high": "高"}

# Channel mapping
CHANNEL_MAP = {"web": "Web", "linkedin": "LinkedIn", "referral": "紹介", "other": "その他"}


def map_temperature(t: str | None) -> str | None:
    """Map English temperature to Japanese label."""
    if not t:
        return None
    return TEMP_MAP.get(t)


def map_channel(ch: str) -> str:
    """Map channel key to display label."""
    return CHANNEL_MAP.get(ch, "その他")


# ---------------------------------------------------------------------------
# Property extractors
# ---------------------------------------------------------------------------

def _extract_title(prop: Any) -> str:
    if not prop:
        return ""
    title = prop.get("title", [])
    if title and len(title) > 0:
        return title[0].get("plain_text", "")
    return ""


def _extract_select(prop: Any) -> str | None:
    if not prop:
        return None
    sel = prop.get("select")
    return sel.get("name") if sel else None


def _extract_status(prop: Any) -> str | None:
    if not prop:
        return None
    status = prop.get("status")
    return status.get("name") if status else None


def _extract_url(prop: Any) -> str | None:
    if not prop:
        return None
    return prop.get("url")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class NotionClient:
    """Notion API wrapper for CRM operations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                from notion_client import Client
            except ImportError:
                raise RuntimeError(
                    "notion-client package not installed. "
                    "Run: pip install notion-client"
                )
            key = self._settings.notion_api_key
            if not key:
                raise RuntimeError("NOTION_API_KEY not configured")
            self._client = Client(auth=key)
        return self._client

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.notion_api_key)

    # --- Inquiries DB ---

    async def create_inquiry(
        self,
        company_name: str,
        email: str,
        subject: str,
        temperature: str | None = None,
        channel: str = "その他",
        received_at: str = "",
        company_research: str | None = None,
        slack_url: str | None = None,
    ) -> str | None:
        """Create a new inquiry page in Notion."""
        if not self.is_configured:
            return None

        db_id = self._settings.notion_inquiries_db_id
        if not db_id:
            logger.warning("NOTION_INQUIRIES_DB_ID not set")
            return None

        try:
            notion = self._get_client()

            properties: dict[str, Any] = {
                "Name": {"title": [{"text": {"content": company_name}}]},
            }

            page = notion.pages.create(
                parent={"database_id": db_id},
                properties=properties,
            )

            # Add details as page content
            blocks: list[dict[str, Any]] = [
                {
                    "object": "block",
                    "type": "heading_3",
                    "heading_3": {
                        "rich_text": [{"text": {"content": "問い合わせ情報"}}]
                    },
                },
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"text": {"content": f"メール: {email}"}}]
                    },
                },
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"text": {"content": f"件名: {subject}"}}]
                    },
                },
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [
                            {"text": {"content": f"温度: {temperature or '不明'}"}}
                        ]
                    },
                },
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [{"text": {"content": f"チャネル: {channel}"}}]
                    },
                },
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {
                        "rich_text": [
                            {"text": {"content": f"受信日: {received_at}"}}
                        ]
                    },
                },
            ]

            if company_research:
                blocks.extend(
                    [
                        {
                            "object": "block",
                            "type": "heading_3",
                            "heading_3": {
                                "rich_text": [{"text": {"content": "会社リサーチ"}}]
                            },
                        },
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [
                                    {"text": {"content": company_research[:2000]}}
                                ]
                            },
                        },
                    ]
                )

            if slack_url:
                blocks.append(
                    {
                        "object": "block",
                        "type": "bookmark",
                        "bookmark": {"url": slack_url},
                    }
                )

            notion.blocks.children.append(block_id=page["id"], children=blocks)

            logger.info(
                "Notion inquiry created",
                extra={"page_id": page["id"], "company": company_name},
            )
            return page["id"]

        except Exception:
            logger.exception("Failed to create Notion inquiry")
            return None

    async def update_inquiry_slack_url(
        self, page_id: str, slack_url: str
    ) -> None:
        """Add Slack link to an inquiry page."""
        if not self.is_configured:
            return
        try:
            notion = self._get_client()
            notion.blocks.children.append(
                block_id=page_id,
                children=[
                    {
                        "object": "block",
                        "type": "bookmark",
                        "bookmark": {"url": slack_url},
                    }
                ],
            )
        except Exception:
            logger.exception("Failed to update Notion inquiry Slack URL")

    # --- Clients DB (取引先リスト) ---

    def _extract_client_record(self, page: dict) -> dict[str, Any]:
        props = page.get("properties", {})
        return {
            "page_id": page["id"],
            "company_name": _extract_title(props.get("名前")),
            "status": _extract_status(props.get("ステータス")),
            "contact_tool": _extract_select(props.get("連絡ツール")),
            "flow_url": _extract_url(props.get("フロー")),
        }

    async def query_inquiries(
        self,
        since_days_ago: int = 7,
    ) -> list[dict[str, Any]]:
        """List recent inquiries from Notion."""
        if not self.is_configured:
            return []

        db_id = self._settings.notion_inquiries_db_id
        if not db_id:
            return []

        try:
            from datetime import datetime, timedelta, timezone

            notion = self._get_client()
            since = (
                datetime.now(timezone.utc) - timedelta(days=since_days_ago)
            ).isoformat()

            res = notion.databases.query(
                database_id=db_id,
                filter={
                    "property": "作成日時",
                    "created_time": {"on_or_after": since},
                },
                page_size=100,
            )

            return [
                {
                    "company_name": _extract_title(
                        page.get("properties", {}).get("Name")
                    ),
                    "created_at": page.get("created_time", ""),
                }
                for page in res.get("results", [])
            ]

        except Exception:
            logger.exception("Failed to list recent inquiries")
            return []

    async def check_client(self, company_name: str) -> dict[str, Any] | None:
        """Lookup an existing client by company name."""
        if not self.is_configured:
            return None

        db_id = self._settings.notion_clients_db_id
        if not db_id:
            logger.warning("NOTION_CLIENTS_DB_ID not set")
            return None

        try:
            notion = self._get_client()
            res = notion.databases.query(
                database_id=db_id,
                filter={
                    "property": "名前",
                    "title": {"contains": company_name},
                },
                page_size=1,
            )

            results = res.get("results", [])
            if not results:
                return None

            return self._extract_client_record(results[0])

        except Exception:
            logger.exception("Failed to find Notion client: %s", company_name)
            return None

    async def list_clients(
        self, status_filter: str | None = None
    ) -> list[dict[str, Any]]:
        """List all clients, optionally filtered by status."""
        if not self.is_configured:
            return []

        db_id = self._settings.notion_clients_db_id
        if not db_id:
            return []

        try:
            notion = self._get_client()
            query_params: dict[str, Any] = {
                "database_id": db_id,
                "page_size": 50,
            }

            if status_filter:
                query_params["filter"] = {
                    "property": "ステータス",
                    "status": {"equals": status_filter},
                }

            res = notion.databases.query(**query_params)
            return [
                self._extract_client_record(page)
                for page in res.get("results", [])
            ]

        except Exception:
            logger.exception("Failed to list Notion clients")
            return []

    async def update_inquiry(
        self,
        page_id: str,
        status: str,
    ) -> None:
        """Update inquiry status by appending a status block."""
        if not self.is_configured:
            return
        try:
            from datetime import datetime, timezone

            notion = self._get_client()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            notion.blocks.children.append(
                block_id=page_id,
                children=[
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "text": {
                                        "content": f"[{now}] ステータス: {status}"
                                    }
                                }
                            ]
                        },
                    }
                ],
            )
        except Exception:
            logger.exception("Failed to update Notion inquiry status")

    async def update_client(
        self,
        company_name: str,
        status: str | None = None,
        contact_tool: str | None = None,
        flow_url: str | None = None,
        memo: str | None = None,
    ) -> dict[str, Any]:
        """Update or create a client record. Returns result info."""
        if not self.is_configured:
            return {"success": False, "error": "Notion not configured"}

        db_id = self._settings.notion_clients_db_id
        if not db_id:
            return {"success": False, "error": "NOTION_CLIENTS_DB_ID not set"}

        try:
            existing = await self.check_client(company_name)
            notion = self._get_client()

            properties: dict[str, Any] = {}
            updated_fields: list[str] = []

            if not existing:
                properties["名前"] = {
                    "title": [{"text": {"content": company_name}}]
                }
            if status:
                properties["ステータス"] = {"status": {"name": status}}
                updated_fields.append(f"ステータス → {status}")
            if contact_tool:
                properties["連絡ツール"] = {"select": {"name": contact_tool}}
                updated_fields.append(f"連絡ツール → {contact_tool}")
            if flow_url:
                properties["フロー"] = {"url": flow_url}
                updated_fields.append(f"フロー → {flow_url}")

            if existing:
                if properties:
                    notion.pages.update(
                        page_id=existing["page_id"],
                        properties=properties,
                    )
                page_id = existing["page_id"]
            else:
                page = notion.pages.create(
                    parent={"database_id": db_id},
                    properties=properties,
                )
                page_id = page["id"]

            # Handle memo as page content append
            if memo:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                notion.blocks.children.append(
                    block_id=page_id,
                    children=[
                        {
                            "object": "block",
                            "type": "callout",
                            "callout": {
                                "icon": {"emoji": "\U0001f4ac"},
                                "rich_text": [
                                    {"text": {"content": f"[{now}] {memo}"}}
                                ],
                            },
                        }
                    ],
                )
                updated_fields.append(f"メモ追記: {memo}")

            logger.info(
                "Client updated",
                extra={
                    "company": company_name,
                    "page_id": page_id,
                    "updated_fields": updated_fields,
                },
            )

            return {
                "success": True,
                "found": existing is not None,
                "page_id": page_id,
                "company_name": company_name,
                "updated_fields": updated_fields,
            }

        except Exception:
            logger.exception("Failed to update client: %s", company_name)
            return {"success": False, "error": "顧客マスタ更新に失敗しました"}

    async def upsert_client(
        self,
        company_name: str,
        data: dict[str, str | None] | None = None,
    ) -> str | None:
        """Create or update a client. Returns page_id."""
        result = await self.update_client(
            company_name,
            status=(data or {}).get("status"),
            contact_tool=(data or {}).get("contact_tool"),
            flow_url=(data or {}).get("flow_url"),
        )
        return result.get("page_id")

    async def add_feedback_to_client(
        self,
        company_name: str,
        feedback: str,
    ) -> bool:
        """Add feedback entry to a client's Notion page."""
        if not self.is_configured:
            return False

        try:
            existing = await self.check_client(company_name)
            if not existing:
                logger.info(
                    "Client not found for feedback, skipping",
                    extra={"company": company_name},
                )
                return False

            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            notion = self._get_client()
            notion.blocks.children.append(
                block_id=existing["page_id"],
                children=[
                    {
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "icon": {"emoji": "\U0001f4ac"},
                            "rich_text": [
                                {"text": {"content": f"[{now}] {feedback}"}}
                            ],
                        },
                    }
                ],
            )
            logger.info(
                "Feedback added to Notion client",
                extra={
                    "company": company_name,
                    "page_id": existing["page_id"],
                },
            )
            return True

        except Exception:
            logger.exception(
                "Failed to add feedback to Notion client: %s", company_name
            )
            return False
