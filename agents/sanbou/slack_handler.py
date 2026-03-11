"""参謀くん - Slack event handlers.

Uses slack_bolt AsyncApp in Socket Mode (same pattern as hisho).
Ported from kashikabot's Bolt listeners.
"""

from __future__ import annotations

import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from agents.sanbou.brain import Brain
from agents.sanbou.config import Settings
from agents.sanbou.db import Database
from agents.sanbou.reporter import Reporter, activity_chart, contribution_chart
from agents.sanbou.team_monitor import TeamMonitor, is_gyoumu_report

logger = logging.getLogger(__name__)


def create_slack_app(
    settings: Settings,
    brain: Brain,
    db: Database,
    team_monitor: TeamMonitor,
    reporter: Reporter,
) -> AsyncApp:
    """Create and configure the Slack bot for 参謀くん."""
    app = AsyncApp(
        token=settings.sanbou_slack_bot_token,
        signing_secret=settings.sanbou_slack_signing_secret,
    )

    shadow_mode = settings.sanbou_shadow_mode
    pulse_channel = settings.sanbou_pulse_channel
    own_bot_id = settings.sanbou_bot_id

    # ------------------------------------------------------------------
    # Message handler (passive ingestion + active reply)
    # ------------------------------------------------------------------

    @app.event("message")
    async def handle_message(event, client: AsyncWebClient):
        """Ingest all messages from monitored channels.
        Reply when mentioned or in active threads.
        """
        # Ignore own messages
        bot_id = event.get("bot_id", "")
        if bot_id == own_bot_id:
            return

        text = event.get("text", "") or ""
        if not text:
            return

        channel = event.get("channel", "")

        # Only monitor specified channels
        if not team_monitor.should_monitor(channel):
            return

        is_bot_message = bool(bot_id)
        is_silent = team_monitor.is_silent(channel)

        # Check for 業務報告
        if is_bot_message and is_gyoumu_report(text):
            logger.info("業務報告 detected in %s ts=%s", channel, event.get("ts"))
            try:
                summary = await brain.analyze_report(text)
                if not is_silent and not shadow_mode:
                    await client.chat_postMessage(
                        channel=channel,
                        thread_ts=event.get("ts"),
                        text=summary,
                    )
                    logger.info("業務報告 analysis posted to %s", channel)
                elif shadow_mode:
                    logger.info(
                        "Shadow mode: would post report analysis to %s: %s",
                        channel,
                        summary[:100],
                    )
                else:
                    logger.info("業務報告 analyzed (silent channel %s)", channel)
            except Exception:
                logger.exception("業務報告 analysis failed")
            return

        # Ignore other bot messages
        if is_bot_message:
            return

        # Ignore subtypes except file_share
        subtype = event.get("subtype", "")
        if subtype and subtype != "file_share":
            return

        user_id = event.get("user", "")
        if not user_id:
            return

        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        has_files = bool(event.get("files"))

        # Ingest the message
        team_monitor.ingest_message(
            slack_ts=ts,
            thread_ts=thread_ts,
            user_id=user_id,
            channel_id=channel,
            text=text,
            has_files=has_files,
        )

        # Check if 参謀くん should reply
        is_mentioned = team_monitor.should_respond(text)

        # Also reply if this is a thread where 参謀くん is already participating
        is_active_thread = False
        thread_context = ""
        if thread_ts and not is_silent:
            try:
                thread_result = await client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=20
                )
                msgs = thread_result.get("messages", [])
                is_active_thread = any(
                    m.get("bot_id") == own_bot_id for m in msgs
                )
                if is_active_thread or is_mentioned:
                    thread_context = "\n".join(
                        (
                            f"参謀くん: {m.get('text', '')}"
                            if m.get("bot_id") == own_bot_id
                            else f"<@{m.get('user', 'unknown')}>: {m.get('text', '')}"
                        )
                        for m in msgs[:-1]
                        if m.get("text")
                    )
            except Exception:
                logger.warning("Could not fetch thread replies", exc_info=True)

        if (is_mentioned or is_active_thread) and not is_silent:
            if shadow_mode:
                logger.info(
                    "Shadow mode: would reply to %s in %s",
                    user_id,
                    channel,
                )
                return
            try:
                reply = await brain.generate_reply(
                    text, user_id, thread_context, channel
                )
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts or ts,
                    text=reply,
                )
                logger.info(
                    "参謀くん replied in %s to %s (mention=%s, active_thread=%s)",
                    channel,
                    user_id,
                    is_mentioned,
                    is_active_thread,
                )
            except Exception:
                logger.exception("参謀くん reply failed")

    # ------------------------------------------------------------------
    # app_mention handler (for direct @参謀くん mentions)
    # ------------------------------------------------------------------

    @app.event("app_mention")
    async def handle_app_mention(event, client: AsyncWebClient):
        """Handle direct @参謀くん mentions."""
        text = event.get("text", "")
        user_id = event.get("user", "")
        channel = event.get("channel", "")
        ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        # Strip bot mention
        text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        if not text:
            if not shadow_mode:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=ts,
                    text="何か用か。",
                )
            return

        # Build thread context if in a thread
        thread_context = ""
        if thread_ts:
            try:
                thread_result = await client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=20
                )
                msgs = thread_result.get("messages", [])
                thread_context = "\n".join(
                    (
                        f"参謀くん: {m.get('text', '')}"
                        if m.get("bot_id") == own_bot_id
                        else f"<@{m.get('user', 'unknown')}>: {m.get('text', '')}"
                    )
                    for m in msgs[:-1]
                    if m.get("text")
                )
            except Exception:
                pass

        if shadow_mode:
            logger.info("Shadow mode: would reply to app_mention from %s", user_id)
            return

        try:
            reply = await brain.generate_reply(text, user_id, thread_context, channel)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts or ts,
                text=reply,
            )
        except Exception:
            logger.exception("app_mention reply failed")

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app.command("/sanbou-profile")
    async def handle_profile(ack, command, respond):
        """Show a member's profile. /sanbou-profile @user"""
        await ack()
        try:
            mention_match = re.search(
                r"<@(\w+)\|?[^>]*>", command.get("text", "")
            )
            target_user = (
                mention_match.group(1) if mention_match else command["user_id"]
            )

            profile = db.get_profile(target_user)
            stats = db.get_daily_stats(target_user, 7)

            if not profile:
                await respond(
                    response_type="ephemeral",
                    text="このメンバーのプロファイルはまだ作成されていない。"
                    "Daily aggregation後に利用可能になる。",
                )
                return

            chart_url = activity_chart(
                [s["date"][5:] for s in stats],
                [s["message_count"] for s in stats],
            )

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{profile.get('display_name') or target_user} のプロファイル",
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Work Style*\n{profile.get('work_style') or 'N/A'}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Strengths*\n{profile.get('strengths') or 'N/A'}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Communication*\n{profile.get('communication_style') or 'N/A'}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Growth Signals*\n{profile.get('growth_signals') or 'N/A'}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Energy*\n{profile.get('energy_indicator') or 'N/A'}",
                    },
                },
            ]

            if stats:
                blocks.append(
                    {
                        "type": "image",
                        "image_url": chart_url,
                        "alt_text": "7-day activity chart",
                    }
                )

            await respond(response_type="ephemeral", blocks=blocks)
        except Exception:
            logger.exception("Profile command failed")
            await respond(
                response_type="ephemeral",
                text="プロファイル取得に失敗した。",
            )

    @app.command("/sanbou-pulse")
    async def handle_pulse(ack, command, respond):
        """Show team pulse (7-day summary). /sanbou-pulse"""
        await ack()
        try:
            users = db.get_distinct_users()
            if not users:
                await respond(
                    response_type="ephemeral", text="まだデータがない。"
                )
                return

            user_stats = []
            for user_id in users:
                stats = db.get_daily_stats(user_id, 7)
                total_msgs = sum(s["message_count"] for s in stats)
                avg_sentiment = (
                    sum(s.get("sentiment_score") or 0 for s in stats)
                    / len(stats)
                    if stats
                    else 0
                )
                if total_msgs > 0:
                    user_stats.append(
                        {
                            "userId": user_id,
                            "messages": total_msgs,
                            "sentiment": avg_sentiment,
                        }
                    )

            chart_url = contribution_chart(
                [u["userId"] for u in user_stats],
                [u["messages"] for u in user_stats],
            )

            summary = "\n".join(
                f"<@{u['userId']}>: {u['messages']} msgs "
                f"(sentiment: {u['sentiment']:.2f})"
                for u in sorted(
                    user_stats, key=lambda x: x["messages"], reverse=True
                )
            )

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "Team Pulse - 直近7日間",
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": summary or "データなし"},
                },
                {
                    "type": "image",
                    "image_url": chart_url,
                    "alt_text": "Team contribution chart",
                },
            ]

            await respond(response_type="ephemeral", blocks=blocks)
        except Exception:
            logger.exception("Pulse command failed")
            await respond(
                response_type="ephemeral",
                text="パルス取得に失敗した。",
            )

    @app.command("/sanbou-wins")
    async def handle_wins(ack, command, respond):
        """Show recent wins/praise. /sanbou-wins"""
        await ack()
        try:
            praises = db.get_recent_praise(10)
            if not praises:
                await respond(
                    response_type="ephemeral",
                    text="まだ成果が検出されていない。",
                )
                return

            praise_list = "\n".join(
                f"- <@{p['user_id']}>: {p['description']} "
                f"({p['detected_at'][:10]})"
                for p in praises
            )

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "Recent Wins",
                        "emoji": True,
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": praise_list},
                },
            ]

            await respond(response_type="ephemeral", blocks=blocks)
        except Exception:
            logger.exception("Wins command failed")
            await respond(
                response_type="ephemeral",
                text="成果取得に失敗した。",
            )

    return app


async def start_slack_app(app: AsyncApp, settings: Settings) -> None:
    """Start the Slack app in Socket Mode."""
    handler = AsyncSocketModeHandler(app, settings.sanbou_slack_app_token)
    await handler.start_async()
