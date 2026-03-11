"""秘書くん - Brain (Claude API reasoning layer).

All decisions flow through here. Modules provide data, brain decides,
modules execute.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic

from agents.hisho.config import Settings
from agents.hisho.gmail_client import EmailMessage

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "hisho_kun_system_prompt_v1.md"
)


@dataclass
class TriageResult:
    priority: Literal["red", "yellow", "green"]
    emoji: str
    reason: str
    suggested_action: str
    summary: str
    needs_draft: bool


@dataclass
class DraftReply:
    subject: str
    body: str
    to: str


# Tool definitions for structured output from Claude
TRIAGE_TOOL = {
    "name": "classify_email",
    "description": "Classify an incoming email by priority and suggest action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "priority": {
                "type": "string",
                "enum": ["red", "yellow", "green"],
                "description": "red=最優先(即通知), yellow=重要(次の報告で), green=低優先(まとめて)",
            },
            "reason": {
                "type": "string",
                "description": "分類理由を1行で",
            },
            "suggested_action": {
                "type": "string",
                "enum": ["draft_reply", "forward_to_sales", "notify_only", "archive"],
                "description": "推奨アクション",
            },
            "summary": {
                "type": "string",
                "description": "えがおに見せる1行サマリー（カジュアルな日本語）",
            },
            "needs_draft": {
                "type": "boolean",
                "description": "返信の下書きを作るべきか",
            },
        },
        "required": ["priority", "reason", "suggested_action", "summary", "needs_draft"],
    },
}

DRAFT_REPLY_TOOL = {
    "name": "compose_draft",
    "description": "Compose a reply draft for the email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Reply subject (usually Re: original subject)",
            },
            "body": {
                "type": "string",
                "description": "Reply body in professional Japanese",
            },
        },
        "required": ["subject", "body"],
    },
}

EMOJI_MAP = {"red": "\U0001f534", "yellow": "\U0001f7e1", "green": "\U0001f7e2"}


class Brain:
    """Claude API reasoning layer for 秘書くん."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        if SYSTEM_PROMPT_PATH.exists():
            return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        logger.warning("System prompt not found at %s, using fallback", SYSTEM_PROMPT_PATH)
        return (
            "あなたは秘書くん。StepAI CEOのえがおの秘書AI。"
            "カジュアルなタメ口で、しっかり者のお姉さん的存在。"
            "メールのトリアージ、カレンダー管理、リマインドが仕事。"
            "メールは絶対に勝手に送らない。下書きまで。"
        )

    def triage_email(self, email: EmailMessage) -> TriageResult:
        """Classify an email by priority and suggest action."""
        user_msg = (
            f"以下のメールを分類して。\n\n"
            f"From: {email.sender} <{email.sender_email}>\n"
            f"Subject: {email.subject}\n"
            f"Date: {email.date}\n"
            f"Snippet: {email.snippet}\n\n"
            f"Body:\n{email.body_text[:2000]}"
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=500,
                system=self._system_prompt,
                tools=[TRIAGE_TOOL],
                tool_choice={"type": "tool", "name": "classify_email"},
                messages=[{"role": "user", "content": user_msg}],
            )

            for block in response.content:
                if block.type == "tool_use" and block.name == "classify_email":
                    data = block.input
                    priority = data["priority"]
                    return TriageResult(
                        priority=priority,
                        emoji=EMOJI_MAP.get(priority, "⚪"),
                        reason=data["reason"],
                        suggested_action=data["suggested_action"],
                        summary=data["summary"],
                        needs_draft=data["needs_draft"],
                    )
        except Exception:
            logger.exception("Failed to triage email: %s", email.subject)

        # Fallback: treat as yellow
        return TriageResult(
            priority="yellow",
            emoji="\U0001f7e1",
            reason="分類できなかった",
            suggested_action="notify_only",
            summary=f"{email.sender}からのメール: {email.subject}",
            needs_draft=False,
        )

    def draft_reply(self, email: EmailMessage, context: str = "") -> DraftReply:
        """Generate a reply draft for an email."""
        user_msg = (
            f"以下のメールへの返信下書きを作成して。\n"
            f"プロフェッショナルだけど温かみのあるビジネス日本語で。\n"
            f"えがお（StepAI CEO）の名前で書く。\n\n"
            f"From: {email.sender} <{email.sender_email}>\n"
            f"Subject: {email.subject}\n"
            f"Body:\n{email.body_text[:2000]}"
        )
        if context:
            user_msg += f"\n\n追加コンテキスト:\n{context}"

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1000,
                system=self._system_prompt,
                tools=[DRAFT_REPLY_TOOL],
                tool_choice={"type": "tool", "name": "compose_draft"},
                messages=[{"role": "user", "content": user_msg}],
            )

            for block in response.content:
                if block.type == "tool_use" and block.name == "compose_draft":
                    data = block.input
                    return DraftReply(
                        subject=data["subject"],
                        body=data["body"],
                        to=email.sender_email,
                    )
        except Exception:
            logger.exception("Failed to draft reply for: %s", email.subject)

        return DraftReply(
            subject=f"Re: {email.subject}",
            body="（下書き生成に失敗しました。手動で作成してください。）",
            to=email.sender_email,
        )

    def generate_morning_report(
        self,
        events: list[dict],
        email_summary: dict,
        pending_tasks: list[str] | None = None,
    ) -> str:
        """Generate a morning report in 秘書くん's voice."""
        user_msg = (
            "朝の報告を作成して。以下のデータを使って、"
            "秘書くんのキャラ（カジュアル、タメ口、しっかり者のお姉さん）で。\n\n"
            f"## 今日のカレンダー\n{json.dumps(events, ensure_ascii=False, default=str)}\n\n"
            f"## メール状況\n{json.dumps(email_summary, ensure_ascii=False)}\n\n"
        )
        if pending_tasks:
            user_msg += f"## 放置タスク\n" + "\n".join(f"- {t}" for t in pending_tasks)

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1500,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            return response.content[0].text
        except Exception:
            logger.exception("Failed to generate morning report")
            return "おはよう〜☀️ 朝の報告の生成に失敗しちゃった。ごめんね！"

    def parse_schedule_request(self, request: str) -> dict:
        """Parse a natural language meeting request."""
        user_msg = (
            f"以下のミーティング設定リクエストを解析して。\n\n"
            f"リクエスト: {request}\n\n"
            "JSON形式で返して:\n"
            '{"company": "会社名", "duration_minutes": 30, '
            '"preferred_dates": ["YYYY-MM-DD"], "notes": "メモ"}'
        )

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = response.content[0].text
            # Extract JSON from response
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
        except Exception:
            logger.exception("Failed to parse schedule request")

        return {"company": "不明", "duration_minutes": 30, "preferred_dates": [], "notes": request}

    def respond_to_message(self, message: str, context: str = "") -> str:
        """General-purpose response for Slack messages."""
        user_msg = message
        if context:
            user_msg = f"コンテキスト:\n{context}\n\nメッセージ:\n{message}"

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1000,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            return response.content[0].text
        except Exception:
            logger.exception("Failed to respond to message")
            return "ごめん、ちょっとエラーが起きちゃった🙏 もう一回言ってくれる？"
