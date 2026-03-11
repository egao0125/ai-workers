"""参謀くん - Brain (Claude API reasoning layer).

Ported from kashikabot's anthropic.ts + analysis modules.
All Claude calls go through here with retry logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import anthropic

from agents.sanbou.config import Settings
from agents.sanbou.db import Database

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "sanbou_kun_system_prompt_v1.md"
)

MAX_RETRIES = 3
BASE_DELAY_S = 1.0


class Brain:
    """Claude API reasoning layer for 参謀くん."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model_sonnet = settings.anthropic_model_sonnet
        self._model_haiku = settings.anthropic_model_haiku
        self._db = db
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        if SYSTEM_PROMPT_PATH.exists():
            return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        logger.warning(
            "System prompt not found at %s, using fallback", SYSTEM_PROMPT_PATH
        )
        return (
            "あなたは参謀くん。StepAIの戦略インテリジェンスAI。"
            "簡潔。指示型。データと根拠で語る。"
            "チーム監視・競合分析・仮説管理が仕事。"
        )

    async def _with_retry(self, operation, context: str):
        """Exponential backoff retry wrapper."""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await asyncio.to_thread(operation)
            except Exception as e:
                last_error = e
                msg = str(e)
                # Don't retry client errors
                if any(code in msg for code in ("400", "401", "403")):
                    raise
                delay = BASE_DELAY_S * (2**attempt)
                logger.warning(
                    "Anthropic API retry [%s] attempt=%d delay=%.1fs: %s",
                    context,
                    attempt + 1,
                    delay,
                    msg,
                )
                await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Report Analysis (port from report-analyzer.ts)
    # ------------------------------------------------------------------

    async def analyze_report(self, report_text: str) -> str:
        """Analyze a 業務報告 post: extract insights, update profiles
        + memories, and return a summary reply.
        """
        profiles = self._db.get_all_profiles()
        memories = self._db.get_memories()
        activity_stats = self._db.get_user_activity_stats(7)

        profile_context = "\n".join(
            self._format_profile_line(p) for p in profiles
        )
        memory_context = "\n".join(
            f"[{m['category']}] {m['key']}: {m['value']}" for m in memories
        )
        existing_direction = next(
            (
                m["value"]
                for m in memories
                if m["category"] == "company" and m["key"] == "direction"
            ),
            "まだなし",
        )
        activity_context = self._format_activity_stats(activity_stats, profiles)

        system = f"""あなたは参謀くん、StepAIの戦略インテリジェンスAI。
簡潔。指示型。データと根拠で語る。無駄な前置き禁止。結論から。

## StepAIとRecoについて
StepAIは音声AIスタートアップ。プロダクト「Reco」は、AI音声オペレーションSaaS。架電/受電をAI自動化。

## 既存のメンバー情報
{profile_context or "まだなし"}

## Slack発言量（過去7日間の実データ）
{activity_context or "データなし"}

## 現在のStepAI方針メモリー（差分更新する）
{existing_direction}

## 参謀くんの記憶
{memory_context or "まだなし"}

## 出力フォーマット（JSON）
{{
  "profileUpdates": [
    {{
      "userId": "U...",
      "displayName": "名前",
      "profile": "全体像を500文字以内。タスク・スキル・特徴を凝縮。",
      "energyIndicator": "🟢 Active / 🟡 Normal / 🔴 Low（Slack発言量データで判断）"
    }}
  ],
  "memories": [
    {{
      "category": "project | person | team | blocker | competitor | hypothesis",
      "key": "短いキー",
      "value": "覚えておくべき内容"
    }}
  ],
  "companyDirection": "StepAIの方向性。差分更新。1000文字以内。",
  "summary": "チームへのコメント（Slack投稿用）"
}}

## summaryのルール
- 短くまとめる。2-3文が上限
- 各メンバーを <@UXXXXXXXX> でメンション
- 戦略的視点でコメント。現状→課題→次のアクション
- 結論から。無駄な装飾なし
- 絵文字は1-2個まで

## profileUpdatesのルール
- profileに「タスク」「スキル」「特徴」を500文字以内で凝縮
- user_idは報告テキスト内の <@UXXXXXXXX> から抽出

## energyIndicatorの判断基準
- Slack発言量データを最優先で参照
- 🟢 Active: 発言量が多い（7日で20件以上）or 複数チャンネルで活発
- 🟡 Normal: 平均的 or データ不足
- 🔴 Low: 発言量が少ない（7日で5件未満）or 体調不良報告"""

        def _call():
            return self._client.messages.create(
                model=self._model_sonnet,
                max_tokens=2000,
                system=system,
                messages=[{"role": "user", "content": f"業務報告:\n{report_text}"}],
            )

        response = await self._with_retry(_call, "analyze-report")

        text_block = next(
            (b for b in response.content if b.type == "text"), None
        )
        if not text_block:
            raise ValueError("No response text from Claude")

        json_match = re.search(r"\{[\s\S]*\}", text_block.text)
        if not json_match:
            raise ValueError("No JSON in Claude response")

        result = json.loads(json_match.group(0))

        # Persist profile updates
        for update in result.get("profileUpdates", []):
            try:
                existing = next(
                    (p for p in profiles if p["user_id"] == update["userId"]),
                    None,
                )
                profile_text = (update.get("profile") or "")[:500]
                self._db.upsert_profile(
                    user_id=update["userId"],
                    display_name=update.get("displayName")
                    or (existing or {}).get("display_name")
                    or update["userId"],
                    work_style=(existing or {}).get("work_style", "データ不足"),
                    strengths=(existing or {}).get("strengths", "データ不足"),
                    communication_style=(existing or {}).get(
                        "communication_style", "データ不足"
                    ),
                    recent_contributions=profile_text,
                    growth_signals=(existing or {}).get(
                        "growth_signals", "データ不足"
                    ),
                    energy_indicator=update.get("energyIndicator")
                    or (existing or {}).get("energy_indicator", "🟡 Neutral"),
                )
                logger.info("Profile updated from report: %s", update["userId"])
            except Exception:
                logger.exception(
                    "Failed to update profile: %s", update.get("userId")
                )

        # Persist memories
        for mem in result.get("memories", []):
            try:
                self._db.upsert_memory(mem["category"], mem["key"], mem["value"])
                logger.info("Memory saved: [%s] %s", mem["category"], mem["key"])
            except Exception:
                logger.exception("Failed to save memory: %s", mem.get("key"))

        # Persist company direction
        if result.get("companyDirection"):
            try:
                direction = result["companyDirection"][:1000]
                self._db.upsert_memory("company", "direction", direction)
                logger.info("Company direction updated (%d chars)", len(direction))
            except Exception:
                logger.exception("Failed to save company direction")

        return result.get("summary", "分析完了。")

    # ------------------------------------------------------------------
    # Profile Building (port from profile-builder.ts)
    # ------------------------------------------------------------------

    async def build_profile(
        self,
        display_name: str,
        recent_messages: list[dict[str, Any]],
        stats: list[dict[str, Any]],
        existing_profile: dict[str, Any] | None,
    ) -> dict[str, str]:
        """Build/update a member profile from recent activity."""
        msg_summary = "\n".join(
            f"[{m['channel_id']}] {m['text'][:200]}"
            for m in recent_messages[:80]
        )
        stats_summary = "\n".join(
            f"{s['date']}: msgs={s['message_count']}, "
            f"channels={s['channels_active']}, "
            f"sentiment={s.get('sentiment_score', 'N/A')}"
            for s in stats
        )
        existing_ctx = ""
        if existing_profile:
            existing_ctx = (
                f"\n前回のプロファイル:\n"
                f"- Work Style: {existing_profile.get('work_style')}\n"
                f"- Strengths: {existing_profile.get('strengths')}\n"
                f"- Communication: {existing_profile.get('communication_style')}\n"
                f"- Energy: {existing_profile.get('energy_indicator')}"
            )

        system = """チームインテリジェンス分析。Slackメッセージと活動データからプロファイルを生成。

JSON形式で返せ:
{
  "workStyle": "働き方の特徴",
  "strengths": "強み・専門分野",
  "communicationStyle": "コミュニケーション特徴",
  "recentContributions": "直近1週間の主な貢献",
  "growthSignals": "成長の兆候",
  "energyIndicator": "🟢 Positive / 🟡 Neutral / 🔴 Low"
}

注意:
- 日本語で書く
- 各フィールドは1-2文で簡潔に
- データがない場合は「データ不足」
- 推測は控えめ。データに基づく"""

        user_msg = (
            f"メンバー: {display_name}{existing_ctx}\n\n"
            f"直近の活動データ:\n{stats_summary or 'なし'}\n\n"
            f"直近のメッセージ:\n{msg_summary or 'なし'}"
        )

        def _call():
            return self._client.messages.create(
                model=self._model_sonnet,
                max_tokens=800,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
            )

        try:
            response = await self._with_retry(_call, "build-profile")
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            if not text_block:
                raise ValueError("No text in response")
            json_match = re.search(r"\{[\s\S]*\}", text_block.text)
            if not json_match:
                raise ValueError("No JSON in response")
            parsed = json.loads(json_match.group(0))
            return {
                "work_style": parsed.get("workStyle", "データ不足"),
                "strengths": parsed.get("strengths", "データ不足"),
                "communication_style": parsed.get(
                    "communicationStyle", "データ不足"
                ),
                "recent_contributions": parsed.get(
                    "recentContributions", "データ不足"
                ),
                "growth_signals": parsed.get("growthSignals", "データ不足"),
                "energy_indicator": parsed.get(
                    "energyIndicator", "🟡 Neutral"
                ),
            }
        except Exception:
            logger.exception("Profile building failed for %s", display_name)
            return {
                "work_style": "データ不足",
                "strengths": "データ不足",
                "communication_style": "データ不足",
                "recent_contributions": "データ不足",
                "growth_signals": "データ不足",
                "energy_indicator": "🟡 Neutral",
            }

    # ------------------------------------------------------------------
    # Sentiment Analysis (port from sentiment.ts) - uses Haiku for speed
    # ------------------------------------------------------------------

    async def analyze_sentiment(self, messages: list[str]) -> float:
        """Analyze sentiment of messages. Returns -1.0 to 1.0."""
        if not messages:
            return 0.0

        sample = "\n---\n".join(messages[:20])

        def _call():
            return self._client.messages.create(
                model=self._model_haiku,
                max_tokens=50,
                system=(
                    "Analyze the overall sentiment of these Slack messages "
                    "from one team member. Return ONLY a JSON object: "
                    '{"score": <number from -1.0 to 1.0>} where -1 is very '
                    "negative/stressed, 0 is neutral, 1 is very positive/energetic."
                ),
                messages=[{"role": "user", "content": sample}],
            )

        try:
            response = await self._with_retry(_call, "sentiment")
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            if not text_block:
                return 0.0
            match = re.search(r'"score"\s*:\s*(-?[\d.]+)', text_block.text)
            if not match:
                return 0.0
            score = float(match.group(1))
            return max(-1.0, min(1.0, score))
        except Exception:
            logger.warning("Sentiment analysis failed", exc_info=True)
            return 0.0

    # ------------------------------------------------------------------
    # Report Generation (daily / weekly) - used by reporter.py
    # ------------------------------------------------------------------

    async def generate_report(
        self,
        *,
        report_type: str,
        start_date: str,
        end_date: str,
        user_summaries: list[dict[str, Any]],
        user_messages: dict[str, list[str]],
    ) -> dict[str, Any]:
        """Generate a daily or weekly team report."""
        is_weekly = report_type == "weekly"
        input_data = "\n".join(
            f"{u['userId']}: {u['messages']} msgs, "
            f"sentiment={u['sentiment']:.2f}, "
            f"sample: {' | '.join((user_messages.get(u['userId'], []))[:5])}"
            for u in user_summaries
        )

        system = (
            "あなたは参謀くん。StepAIの戦略インテリジェンスAI。"
            "チームの{}レポートを生成しろ。\n\n"
            "JSON形式で返せ:\n"
            '{{\n'
            '  "summary": "チーム全体の概要（{}）",\n'
            '  "memberHighlights": [{{"userId": "U...", "highlight": "貢献内容"}}],\n'
            '  "teamWins": ["成果"],\n'
            '  "blockers": ["課題"]\n'
            '}}\n\n'
            "日本語。簡潔。データに基づけ。推測するな。"
        ).format(
            "週次" if is_weekly else "日次",
            "2-3文" if is_weekly else "1-2文、簡潔に",
        )

        def _call():
            return self._client.messages.create(
                model=self._model_sonnet,
                max_tokens=1000 if is_weekly else 500,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"期間: {start_date} 〜 {end_date} ({report_type})\n\n"
                            f"{input_data}"
                        ),
                    }
                ],
            )

        response = await self._with_retry(_call, "generate-report")
        text_block = next(
            (b for b in response.content if b.type == "text"), None
        )
        if not text_block:
            raise ValueError("No response text")

        json_match = re.search(r"\{[\s\S]*\}", text_block.text)
        if not json_match:
            raise ValueError("No JSON in response")

        return json.loads(json_match.group(0))

    # ------------------------------------------------------------------
    # Chat Reply (port from chat-reply.ts)
    # ------------------------------------------------------------------

    async def generate_reply(
        self,
        user_message: str,
        user_id: str,
        thread_context: str = "",
        channel_id: str = "",
    ) -> str:
        """Generate a reply when someone mentions or talks to 参謀くん."""
        profiles = self._db.get_all_profiles()
        memories = self._db.get_memories()
        activity_stats = self._db.get_user_activity_stats(7)

        profile_context = "\n".join(
            self._format_profile_line(p) for p in profiles
        )
        company_direction = next(
            (
                m["value"]
                for m in memories
                if m["category"] == "company" and m["key"] == "direction"
            ),
            "まだデータなし",
        )
        memory_context = "\n".join(
            f"[{m['category']}] {m['key']}: {m['value']}"
            for m in memories
            if not (m["category"] == "company" and m["key"] == "direction")
        )
        activity_context = self._format_activity_stats(activity_stats, profiles)

        system = f"""{self._system_prompt}

## チームメンバー情報
{profile_context or "まだなし"}

## Slack発言量（過去7日間）
{activity_context or "データなし"}

## StepAI方針
{company_direction}

## 記憶
{memory_context or "まだなし"}

## チャット返信ルール
- 絵文字は控えめ（1-2個まで）
- データで語れ。根拠がなければ「データ不足、調査中」
- 質問は必須ではない。無理に質問しない
- 雑談は短く返す（1-3文）
- メンバーの情報を聞かれたらプロファイルと記憶から答える
- 話しかけてきた人の user_id は {user_id}"""

        messages: list[dict[str, Any]] = []
        if thread_context:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"【スレッドの流れ】\n{thread_context}\n\n"
                        f"【最新メッセージ】\n{user_message}"
                    ),
                }
            )
        else:
            messages.append({"role": "user", "content": user_message})

        def _call():
            return self._client.messages.create(
                model=self._model_sonnet,
                max_tokens=1200,
                system=system,
                messages=messages,
            )

        try:
            response = await self._with_retry(_call, "chat-reply")
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            if not text_block:
                return "了解。"
            return text_block.text
        except Exception:
            logger.exception("Chat reply failed")
            return "エラーが発生した。もう一度試せ。"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_profile_line(p: dict[str, Any]) -> str:
        parts = [f"{p.get('display_name') or p['user_id']} ({p['user_id']})"]
        if p.get("role"):
            parts.append(f"役割: {p['role']}")
        rc = p.get("recent_contributions")
        if rc and rc != "データ不足":
            parts.append(f"プロフィール: {rc}")
        if p.get("energy_indicator"):
            parts.append(f"エネルギー: {p['energy_indicator']}")
        return " | ".join(parts)

    @staticmethod
    def _format_activity_stats(
        stats: list[dict[str, Any]], profiles: list[dict[str, Any]]
    ) -> str:
        lines = []
        for a in stats:
            profile = next(
                (p for p in profiles if p["user_id"] == a["user_id"]), None
            )
            name = (profile or {}).get("display_name") or a["user_id"]
            lines.append(
                f"{name}: {a['message_count']}件/7日, "
                f"スレッド{a['thread_count']}, "
                f"チャンネル{a['channels_active']}"
            )
        return "\n".join(lines)
