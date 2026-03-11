"""セールスくん - Multi-turn conversation with tool use.

Ports reasoner.ts from maakun. Claude Sonnet with tool_use, up to 5 rounds.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from agents.sales.classifier import ActionType, ClassificationResult
from agents.sales.config import Settings

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "sales_kun_system_prompt_v1.md"
)


@dataclass
class SlackContext:
    user_id: str
    channel_id: str


@dataclass
class ReasonerResult:
    reply: str
    tools_used: list[str] = field(default_factory=list)
    needs_human_review: bool = False


def _load_system_prompt() -> str:
    """Load system prompt from file, with fallback."""
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    logger.warning("System prompt not found at %s, using fallback", SYSTEM_PROMPT_PATH)
    return (
        "あなたはセールスくん。StepAIの営業エージェント。\n"
        "Recoのパイプラインを回す。リサーチ、アウトリーチ、返信対応、"
        "フォローアップ、パイプライン管理を自律で実行する。"
    )


def _is_high_stakes(classification: ClassificationResult) -> bool:
    high_stakes = {ActionType.ESCALATION_NEEDED, ActionType.CLIENT_FEEDBACK}
    return classification.action_type in high_stakes or classification.confidence < 0.85


def _detect_human_escalation(reply: str) -> bool:
    markers = ["Human review recommended", "要確認", "エスカレーション", "\u26a0\ufe0f"]
    return any(m in reply for m in markers)


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "research_company",
        "description": (
            "Research a company by name or email domain. Returns industry, size, "
            "location, call center info, and vertical match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "会社名"},
                "email_domain": {"type": "string", "description": "メールドメイン"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "check_client",
        "description": "Search the Notion client database by company name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "会社名"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "register_inquiry",
        "description": (
            "Register a new inquiry in the Notion database. Automatically researches "
            "the company and checks if they're an existing client."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "会社名"},
                "contact_name": {"type": "string", "description": "問い合わせ者の名前"},
                "contact_email": {"type": "string", "description": "メールアドレス"},
                "subject": {"type": "string", "description": "件名・要件"},
                "body": {"type": "string", "description": "問い合わせ本文"},
                "channel": {
                    "type": "string",
                    "enum": ["web", "linkedin", "referral", "other"],
                    "description": "流入チャネル",
                },
                "temperature": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "温度感",
                },
            },
            "required": [
                "company_name",
                "contact_name",
                "contact_email",
                "subject",
                "body",
            ],
        },
    },
    {
        "name": "get_open_inquiries",
        "description": "Get all open (unreplied) inquiries, optionally filtered by minimum hours open.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_hours_open": {
                    "type": "number",
                    "description": "Minimum hours since inquiry was received",
                },
            },
            "required": [],
        },
    },
    {
        "name": "check_reply_status",
        "description": "Check if a specific inquiry has been replied to.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inquiry_id": {"type": "string", "description": "Inquiry ID"},
            },
            "required": ["inquiry_id"],
        },
    },
    {
        "name": "get_kpi_summary",
        "description": "Get KPI summary for a given period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["daily", "weekly", "monthly"],
                    "description": "Reporting period",
                },
            },
            "required": ["period"],
        },
    },
    {
        "name": "log_feedback",
        "description": "Log client feedback. Records it and returns pattern detection info.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client": {"type": "string", "description": "Client/company name"},
                "category": {
                    "type": "string",
                    "enum": [
                        "positive",
                        "feature_request",
                        "bug",
                        "complaint",
                        "process_improvement",
                    ],
                    "description": "Feedback category",
                },
                "content": {"type": "string", "description": "Feedback content"},
            },
            "required": ["client", "category", "content"],
        },
    },
    {
        "name": "get_feedback_patterns",
        "description": "Get recurring feedback patterns (2+ similar items).",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_frequency": {
                    "type": "number",
                    "description": "Minimum frequency to include (default: 2)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "draft_reply",
        "description": "Generate a reply draft for an inquiry. NEVER sends — draft only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inquiry_id": {"type": "string", "description": "Inquiry ID"},
                "tone": {
                    "type": "string",
                    "enum": ["formal", "friendly", "urgent"],
                    "description": "Tone of the reply",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context for the reply",
                },
            },
            "required": ["inquiry_id"],
        },
    },
    {
        "name": "list_clients",
        "description": "List all clients from Notion. Optionally filter by status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["リード", "トライアル", "契約中", "解約"],
                    "description": "ステータスでフィルタ",
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_client",
        "description": "Update a client record in Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "対象の会社名"},
                "status": {"type": "string", "description": "ステータス変更"},
                "contact_tool": {"type": "string", "description": "連絡ツール変更"},
                "flow_url": {"type": "string", "description": "フローURL設定"},
                "memo": {"type": "string", "description": "メモ追記"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "do_not_send_check",
        "description": (
            "Run Do-Not-Send safety checks before outreach. Checks: already approached, "
            "egao direct contact, support open, recently churned, duplicate company, bad timing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "対象の会社名"},
                "contact_name": {"type": "string", "description": "担当者名"},
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "tier_classify",
        "description": (
            "Classify a company into Tier1/2/3. Tier1=enterprise (egao approval), "
            "Tier2=mid-market (autonomous), Tier3=BPO (fast close)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "会社名"},
                "industry": {"type": "string", "description": "業種"},
                "estimated_revenue": {"type": "string", "description": "推定月額"},
                "seat_count": {"type": "number", "description": "コールセンター席数"},
            },
            "required": ["company_name"],
        },
    },
]


class Reasoner:
    """Multi-turn Claude Sonnet conversation with tool dispatch."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.reasoner_model
        self._system_prompt = _load_system_prompt()
        self._settings = settings
        # Tool dispatcher will be set externally
        self._dispatch_tool = None

    def set_tool_dispatcher(self, dispatcher) -> None:
        """Set the tool dispatch function (avoids circular import)."""
        self._dispatch_tool = dispatcher

    async def run(
        self,
        message_text: str,
        classification: ClassificationResult,
        thread_context: str = "",
        sender_name: str = "",
        slack_context: SlackContext | None = None,
    ) -> ReasonerResult:
        """Run the reasoner with tool use loop (up to MAX_TOOL_ROUNDS)."""
        try:
            tools_used: list[str] = []

            high_stakes_note = ""
            if _is_high_stakes(classification):
                high_stakes_note = (
                    "\n注意: クレーム・バグ・エスカレーションの可能性あり。慎重に対応して。"
                )

            thread_section = ""
            if thread_context:
                thread_section = f"\nスレッドの流れ:\n{thread_context}\n---"

            sender = sender_name or "チームメンバー"

            user_content = (
                f"Slackの会話:{thread_section}\n\n"
                f"{sender}: {message_text}"
                f"{high_stakes_note}\n"
                f"チームメイトとしてアクションポイントや改善提案を出して。短く。"
            )

            messages: list[dict[str, Any]] = [
                {"role": "user", "content": user_content},
            ]

            for round_num in range(MAX_TOOL_ROUNDS):
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=600,
                    system=self._system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )

                # Check if done
                if response.stop_reason == "end_turn":
                    text_block = next(
                        (b for b in response.content if b.type == "text"), None
                    )
                    reply = text_block.text if text_block else ""
                    logger.info(
                        "Reasoner complete",
                        extra={"rounds": round_num + 1, "tools_used": tools_used},
                    )
                    return ReasonerResult(
                        reply=reply,
                        tools_used=tools_used,
                        needs_human_review=_detect_human_escalation(reply),
                    )

                # Extract tool_use blocks
                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

                if not tool_use_blocks:
                    text_block = next(
                        (b for b in response.content if b.type == "text"), None
                    )
                    reply = text_block.text if text_block else ""
                    return ReasonerResult(
                        reply=reply,
                        tools_used=tools_used,
                        needs_human_review=_detect_human_escalation(reply),
                    )

                # Add assistant response to messages
                messages.append({"role": "assistant", "content": response.content})

                # Dispatch tools and collect results
                tool_results = []
                for block in tool_use_blocks:
                    tools_used.append(block.name)
                    if self._dispatch_tool:
                        result = await self._dispatch_tool(
                            block.name,
                            block.input,
                            slack_context,
                        )
                    else:
                        result = json.dumps(
                            {"success": False, "error": "Tool dispatcher not configured"}
                        )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

                messages.append({"role": "user", "content": tool_results})

            logger.warning("Reasoner hit max tool rounds: %d", MAX_TOOL_ROUNDS)
            return ReasonerResult(reply="", tools_used=tools_used)

        except Exception:
            logger.exception("Reasoner failed")
            return ReasonerResult(reply="", tools_used=[])
