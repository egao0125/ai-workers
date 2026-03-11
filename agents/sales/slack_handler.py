"""セールスくん - Slack event handlers.

Uses slack_bolt AsyncApp in Socket Mode (same pattern as hisho).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from agents.sales.classifier import ActionType, ClassificationResult, Classifier
from agents.sales.config import Settings
from agents.sales.feedback import FeedbackDetector
from agents.sales.reasoner import Reasoner, SlackContext

logger = logging.getLogger(__name__)

# Debounce: track recently replied threads
_recent_thread_replies: dict[str, float] = {}
THREAD_DEBOUNCE_SEC = 10.0

# User name cache
_user_name_cache: dict[str, str] = {}


async def _resolve_user_name(client: AsyncWebClient, user_id: str) -> str:
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    try:
        result = await client.users_info(user=user_id)
        user = result.get("user", {})
        profile = user.get("profile", {})
        name = (
            profile.get("display_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
        _user_name_cache[user_id] = name
        return name
    except Exception:
        return user_id


async def _fetch_thread_context(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    own_bot_id: str = "",
) -> dict[str, Any]:
    """Fetch thread context. Returns {context: str, has_other_bot: bool}."""
    try:
        result = await client.conversations_replies(
            channel=channel, ts=thread_ts, limit=20
        )
        messages = result.get("messages", [])
        if len(messages) <= 1:
            return {"context": "", "has_other_bot": False}

        lines: list[str] = []
        has_other_bot = False

        for m in messages[:-1]:  # All except the current message
            text = m.get("text", "")
            if not text:
                continue

            bot_id = m.get("bot_id", "")
            if bot_id:
                if own_bot_id and bot_id == own_bot_id:
                    name = "セールスくん"
                else:
                    name = "他Bot"
                    has_other_bot = True
            else:
                user_id = m.get("user", "unknown")
                name = await _resolve_user_name(client, user_id)

            lines.append(f"{name}: {text}")

        return {"context": "\n".join(lines), "has_other_bot": has_other_bot}

    except Exception:
        logger.warning("Failed to fetch thread context")
        return {"context": "", "has_other_bot": False}


def create_slack_app(
    settings: Settings,
    classifier: Classifier,
    reasoner: Reasoner,
    feedback_detector: FeedbackDetector,
    pipeline=None,
) -> AsyncApp:
    """Create and configure the Slack bot."""
    app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    monitored = settings.monitored_channel_set
    own_bot_id = settings.sales_bot_id
    escalation_user = settings.escalation_user_id
    shadow_mode = settings.shadow_mode

    # --- Bot mention patterns ---
    MENTION_PATTERN = re.compile(
        r"セールスくん|せーるすくん|sales-kun|saleskun", re.IGNORECASE
    )
    OTHER_BOT_PATTERN = re.compile(
        r"サポ君|さぽ君|さぽくん|秘書くん|ひしょくん|参謀くん|さんぼうくん",
        re.IGNORECASE,
    )
    STANDBY_PATTERN = re.compile(r"待機|動かない|話さない|黙る|静観|声かけるまで")

    # --- Event Handlers ---

    @app.event("message")
    async def handle_message(event, client):
        """Monitor messages in sales channels."""
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return

        bot_id = event.get("bot_id", "")
        is_own_bot = own_bot_id and bot_id == own_bot_id
        is_other_bot = bool(bot_id) and not is_own_bot

        # Always ignore own messages
        if is_own_bot:
            return

        text = event.get("text", "")
        if not text:
            return

        channel = event.get("channel", "")

        # Only process messages in monitored channels (empty set = monitor all)
        if monitored and channel not in monitored:
            # Still handle DMs
            if event.get("channel_type") != "im":
                return

        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        user_id = event.get("user", "unknown")

        sender_name = "他Bot" if is_other_bot else await _resolve_user_name(client, user_id)

        logger.info(
            "Processing message",
            extra={
                "channel": channel,
                "ts": ts,
                "text_length": len(text),
                "in_thread": bool(thread_ts),
                "sender": sender_name,
                "is_bot": is_other_bot,
            },
        )

        # Check if セールスくん is mentioned
        is_mentioned = not is_other_bot and MENTION_PATTERN.search(text) is not None

        # If addressed to another bot and NOT to us, skip
        addressed_to_other = OTHER_BOT_PATTERN.search(text) is not None and not is_mentioned
        if addressed_to_other:
            logger.info("Message addressed to another bot, skipping")
            return

        # Fetch thread context
        thread_info = {"context": "", "has_other_bot": False}
        if thread_ts:
            thread_info = await _fetch_thread_context(
                client, channel, thread_ts, own_bot_id
            )

        is_in_active_thread = thread_ts and (
            "セールスくん" in thread_info["context"]
            or "せーるすくん" in thread_info["context"]
        )

        # For bot messages: only respond in discussion mode
        if is_other_bot:
            if is_in_active_thread:
                has_discussion = re.search(
                    r"議論して|議論しよう|話し合って|ディスカッション",
                    thread_info["context"],
                )
                if not has_discussion:
                    return
                own_replies = thread_info["context"].count("セールスくん:")
                if own_replies >= 5:
                    logger.info("Bot safety cap reached")
                    return
            else:
                return

        # Check standby mode
        if is_in_active_thread and not is_mentioned:
            context_lines = thread_info["context"].split("\n")
            last_own = None
            for line in reversed(context_lines):
                if line.startswith("セールスくん:"):
                    last_own = line
                    break
            if last_own and STANDBY_PATTERN.search(last_own):
                logger.info("セールスくん is in standby mode, skipping")
                return

        # Direct mention or active thread -> use reasoner
        if is_mentioned or is_in_active_thread:
            import time as _time
            thread_key = thread_ts or ts
            last_reply = _recent_thread_replies.get(thread_key, 0)
            if _time.time() - last_reply < THREAD_DEBOUNCE_SEC:
                logger.info("Debounce: skipping recent thread")
                return
            _recent_thread_replies[thread_key] = _time.time()

            logger.info("Direct mention or active thread - using reasoner")

            classification = ClassificationResult(
                should_act=True,
                action_type=ActionType.CLIENT_FEEDBACK,
                confidence=1.0,
                reasoning="Direct mention or thread follow-up",
            )

            slack_ctx = SlackContext(user_id=user_id, channel_id=channel)
            result = await reasoner.run(
                text,
                classification,
                thread_info["context"],
                sender_name,
                slack_ctx,
            )

            if result.reply and "[NO_REPLY]" not in result.reply:
                reply_text = result.reply
                if result.needs_human_review and escalation_user:
                    reply_text += f"\n\ncc <@{escalation_user}>"

                if not shadow_mode:
                    try:
                        await client.chat_postMessage(
                            channel=channel,
                            thread_ts=thread_ts or ts,
                            text=reply_text,
                        )
                    except Exception:
                        logger.exception("Failed to post reply")
                else:
                    logger.info(
                        "SHADOW_MODE: Would post reply: %s",
                        reply_text[:100],
                    )
            return

        # Classify for feedback detection
        classification = classifier.classify_message(text)

        if not classification.should_act:
            return

        # Process feedback
        if classification.action_type == ActionType.CLIENT_FEEDBACK:
            feedback = feedback_detector.detect_feedback(text, sender_name)
            if feedback:
                log_result = feedback_detector.log_feedback(
                    feedback["client"],
                    feedback["category"],
                    feedback["content"],
                )

                # Alert on bugs/complaints or patterns
                data = log_result.get("data", {})
                should_alert = (
                    feedback["category"] in ("bug", "complaint")
                    or data.get("is_pattern", False)
                )

                if should_alert and not shadow_mode:
                    category_emoji = {
                        "positive": "💚",
                        "feature_request": "💡",
                        "bug": "🐛",
                        "complaint": "⚠️",
                        "process_improvement": "🔧",
                    }
                    emoji = category_emoji.get(feedback["category"], "💬")

                    alert_lines = [
                        f"{emoji} クライアントFB: {feedback['client']}",
                        f"「{feedback['content']}」",
                    ]

                    if data.get("is_pattern"):
                        patterns = feedback_detector.find_patterns()
                        pattern_data = patterns.get("data", [])
                        matching = next(
                            (p for p in pattern_data if p["category"] == feedback["category"]),
                            None,
                        )
                        if matching:
                            alert_lines.extend([
                                "",
                                f"💬 FBパターン検知: この指摘は *{data['same_category_count']}件目*",
                            ])
                            for entry in matching["entries"][-3:]:
                                alert_lines.append(
                                    f"  - {entry['detected_at'][:10]} "
                                    f"{entry['client']}: 「{entry['content']}」"
                                )
                            alert_lines.extend([
                                "",
                                "🔧 提案: プロダクトバックログに入れて優先度を検討する？",
                            ])

                    try:
                        await client.chat_postMessage(
                            channel=channel,
                            text="\n".join(alert_lines),
                        )
                    except Exception:
                        logger.exception("Failed to post feedback alert")

                    # Escalate bugs and complaints
                    if feedback["category"] in ("bug", "complaint") and escalation_user:
                        label = "バグ報告" if feedback["category"] == "bug" else "クレーム"
                        try:
                            await client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts or ts,
                                text=(
                                    f"⚠️ <@{escalation_user}> "
                                    f"{feedback['client']}から{label}です。確認お願いします。"
                                ),
                            )
                        except Exception:
                            logger.exception("Failed to post escalation")

    @app.event("app_mention")
    async def handle_app_mention(event, client):
        """Handle @セールスくん mentions (defers to message handler logic)."""
        # The message event handler already handles mentions in monitored channels.
        # This handler catches mentions in non-monitored channels.
        text = event.get("text", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        user_id = event.get("user", "unknown")

        # Strip bot mention
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if not text:
            if not shadow_mode:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts or ts,
                    text="何か用？パイプラインの状況聞きたい？",
                )
            return

        sender_name = await _resolve_user_name(client, user_id)

        thread_context = ""
        if thread_ts:
            info = await _fetch_thread_context(client, channel, thread_ts, own_bot_id)
            thread_context = info["context"]

        classification = ClassificationResult(
            should_act=True,
            action_type=ActionType.CLIENT_FEEDBACK,
            confidence=1.0,
            reasoning="Direct @mention",
        )

        slack_ctx = SlackContext(user_id=user_id, channel_id=channel)
        result = await reasoner.run(
            text,
            classification,
            thread_context,
            sender_name,
            slack_ctx,
        )

        if result.reply and "[NO_REPLY]" not in result.reply:
            reply_text = result.reply
            if result.needs_human_review and escalation_user:
                reply_text += f"\n\ncc <@{escalation_user}>"

            if not shadow_mode:
                try:
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts or ts,
                        text=reply_text,
                    )
                except Exception:
                    logger.exception("Failed to post mention reply")
            else:
                logger.info("SHADOW_MODE: Would post reply: %s", reply_text[:100])

    return app


async def start_slack_app(app: AsyncApp, settings: Settings) -> None:
    """Start the Slack app in Socket Mode."""
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    await handler.start_async()
