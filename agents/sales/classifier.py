"""セールスくん - Fast classification layer.

Uses Claude Haiku for speed. Ports classifier.ts from maakun.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal

import anthropic

from agents.sales.config import Settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NEW_INQUIRY = "new_inquiry"
    SPAM_EMAIL = "spam_email"
    SALES_EMAIL = "sales_email"
    EXISTING_CLIENT_EMAIL = "existing_client_email"
    CLIENT_FEEDBACK = "client_feedback"
    BOTTLENECK_ALERT = "bottleneck_alert"
    KPI_TRIGGER = "kpi_trigger"
    ESCALATION_NEEDED = "escalation_needed"
    NOT_RELEVANT = "not_relevant"


@dataclass
class ClassificationResult:
    should_act: bool
    action_type: ActionType
    confidence: float
    reasoning: str


@dataclass
class EmailClassification:
    action_type: ActionType
    temperature: Literal["low", "medium", "high"] | None
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EMAIL_CLASSIFIER_PROMPT = """You classify incoming emails for StepAI (AI-powered call center SaaS).
Decide the email type and temperature.

Action types:
- new_inquiry: 新規問い合わせ（製品に興味、デモ希望、資料請求、見積もり依頼）
- spam_email: スパム、無関係なメール
- sales_email: 他社からの営業メール（「連携しませんか」「サービスのご紹介」等）
- existing_client_email: 既存クライアントからのメール（フィードバック、サポート依頼等）
- not_relevant: 分類不能

Temperature (for new_inquiry and existing_client_email only):
- low: 資料請求のみ、一般的な問い合わせ
- medium: デモ希望、具体的な質問、機能確認
- high: すぐ導入したい、見積もり依頼、緊急、クレーム

Respond with ONLY valid JSON (no markdown, no code fences):
{
  "actionType": "<one of the types above>",
  "temperature": "<low|medium|high|null>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<brief explanation>"
}

問い合わせの可能性があるものはなるべく拾う。明らかなスパムだけspam_emailにする。
「パートナー提案」系は sales_email だが、BPOパートナーは new_inquiry 扱い。
日本語・英語どちらも対応。"""

FEEDBACK_CLASSIFIER_PROMPT = """You detect client feedback in Slack messages for StepAI CS team.
Look for any mention of client/customer feedback, complaints, praise, or feature requests.

Action types:
- client_feedback: クライアントからのFB、フィードバック、感想、不満、要望、バグ報告が含まれている
- not_relevant: クライアントFBではない通常のチーム会話

Respond with ONLY valid JSON (no markdown, no code fences):
{
  "actionType": "<one of the types above>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<brief explanation>"
}

チームメンバーがクライアントの声を共有している場合も検知する。
「〜って言ってた」「〜の反応は」「〜からFBが」等のパターンに注目。"""

TEMPERATURE_CLASSIFIER_PROMPT = """You classify inquiry temperature for StepAI sales pipeline.

Temperature levels:
- hot: すぐ導入したい、見積もり依頼、予算確保済み、決裁者から直接問い合わせ、緊急
- warm: デモ希望、具体的な質問、比較検討中、機能確認
- cold: 資料請求のみ、一般的な問い合わせ、情報収集段階

Respond with ONLY valid JSON:
{
  "temperature": "<hot|warm|cold>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<brief explanation>"
}"""


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class Classifier:
    """Fast classification layer using Claude Haiku."""

    def __init__(self, settings: Settings) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.classifier_model
        self._threshold = settings.classifier_confidence_threshold

    def _call_haiku(self, system: str, user_msg: str) -> str:
        """Call Haiku with JSON prefill trick for reliable parsing."""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=256,
            messages=[
                {"role": "user", "content": f"{system}\n\n{user_msg}"},
                {"role": "assistant", "content": "{"},
            ],
        )
        content = response.content[0]
        if content.type != "text":
            raise ValueError("Unexpected response type from classifier")
        raw = "{" + content.text
        # Strip any markdown fences
        raw = raw.replace("```json", "").replace("```", "").strip()
        return raw

    def classify_email(
        self,
        subject: str,
        body: str,
        sender_email: str,
    ) -> EmailClassification:
        """Classify an incoming email (Flow A)."""
        try:
            user_msg = (
                f"Email to classify:\n"
                f"From: {sender_email}\n"
                f"Subject: {subject}\n"
                f"Body: {body[:1000]}"
            )
            raw = self._call_haiku(EMAIL_CLASSIFIER_PROMPT, user_msg)
            parsed = json.loads(raw)

            result = EmailClassification(
                action_type=ActionType(parsed["actionType"]),
                temperature=parsed.get("temperature"),
                confidence=parsed.get("confidence", 0.0),
                reasoning=parsed.get("reasoning", ""),
            )
            logger.info(
                "Email classification complete",
                extra={
                    "action_type": result.action_type.value,
                    "temperature": result.temperature,
                    "confidence": result.confidence,
                },
            )
            return result

        except Exception:
            logger.exception("Email classifier failed")
            return EmailClassification(
                action_type=ActionType.NOT_RELEVANT,
                temperature=None,
                confidence=0.0,
                reasoning="Classification failed - graceful fallback",
            )

    def classify_message(self, text: str) -> ClassificationResult:
        """Classify a Slack message for feedback detection (Flow D)."""
        try:
            user_msg = f'Message to classify:\n"{text}"'
            raw = self._call_haiku(FEEDBACK_CLASSIFIER_PROMPT, user_msg)
            parsed = json.loads(raw)

            action_type = ActionType(parsed["actionType"])
            confidence = parsed.get("confidence", 0.0)

            result = ClassificationResult(
                should_act=(
                    action_type != ActionType.NOT_RELEVANT
                    and confidence >= self._threshold
                ),
                action_type=action_type,
                confidence=confidence,
                reasoning=parsed.get("reasoning", ""),
            )
            logger.info(
                "Slack classification complete",
                extra={
                    "action_type": result.action_type.value,
                    "confidence": result.confidence,
                    "should_act": result.should_act,
                },
            )
            return result

        except Exception:
            logger.exception("Slack classifier failed")
            return ClassificationResult(
                should_act=False,
                action_type=ActionType.NOT_RELEVANT,
                confidence=0.0,
                reasoning="Classification failed - graceful fallback",
            )

    def classify_inquiry_temperature(
        self, subject: str, body: str
    ) -> dict:
        """Score an inquiry as hot/warm/cold."""
        try:
            user_msg = (
                f"Inquiry to classify:\n"
                f"Subject: {subject}\n"
                f"Body: {body[:1000]}"
            )
            raw = self._call_haiku(TEMPERATURE_CLASSIFIER_PROMPT, user_msg)
            parsed = json.loads(raw)
            return {
                "temperature": parsed.get("temperature", "cold"),
                "confidence": parsed.get("confidence", 0.0),
                "reasoning": parsed.get("reasoning", ""),
            }
        except Exception:
            logger.exception("Temperature classifier failed")
            return {
                "temperature": "cold",
                "confidence": 0.0,
                "reasoning": "Classification failed",
            }
