"""セールスくん - Entry point.

Wires all modules together and starts the bot.

Usage:
    python -m agents.sales.main
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any

from agents.sales.classifier import Classifier
from agents.sales.config import get_settings
from agents.sales.feedback import FeedbackDetector, FeedbackStore
from agents.sales.gmail_client import GmailClient
from agents.sales.notion_client import NotionClient
from agents.sales.pipeline import Pipeline, tier_classify, do_not_send_check
from agents.sales.reasoner import Reasoner, SlackContext
from agents.sales.research import research_company
from agents.sales.scheduler import setup_scheduler
from agents.sales.slack_handler import create_slack_app, start_slack_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sales")


# ---------------------------------------------------------------------------
# Tool dispatcher (connects reasoner to actual tool implementations)
# ---------------------------------------------------------------------------

async def _build_tool_dispatcher(
    settings,
    pipeline: Pipeline,
    notion: NotionClient | None,
    feedback_detector: FeedbackDetector,
):
    """Build the tool dispatch function for the reasoner."""

    async def dispatch_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        slack_context: SlackContext | None = None,
    ) -> str:
        """Route tool calls to implementations."""
        logger.info("Dispatching tool: %s", tool_name)

        try:
            if tool_name == "research_company":
                result = await research_company(
                    tool_input.get("company_name", ""),
                    tool_input.get("email_domain"),
                    settings,
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "check_client":
                if not notion:
                    return json.dumps({"success": False, "error": "Notion not configured"})
                client_record = await notion.check_client(
                    tool_input.get("company_name", "")
                )
                if client_record:
                    return json.dumps(
                        {"success": True, "data": {"is_existing": True, **client_record}},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {"success": True, "data": {"is_existing": False}},
                    ensure_ascii=False,
                )

            elif tool_name == "register_inquiry":
                if not notion:
                    return json.dumps({"success": False, "error": "Notion not configured"})

                company = tool_input.get("company_name", "")
                email = tool_input.get("contact_email", "")
                domain = email.split("@")[1] if "@" in email else ""

                # Research
                res = await research_company(company, domain, settings)
                research_data = res.get("data") if res.get("success") else None

                # Check existing
                existing = await notion.check_client(
                    (research_data or {}).get("company_name", company)
                )

                # Build research summary
                summary_parts = []
                if research_data:
                    if research_data.get("industry"):
                        summary_parts.append(f"業種: {research_data['industry']}")
                    if research_data.get("size"):
                        summary_parts.append(f"規模: {research_data['size']}")
                    if research_data.get("location"):
                        summary_parts.append(f"所在地: {research_data['location']}")
                    if research_data.get("call_center_info"):
                        summary_parts.append(f"CC: {research_data['call_center_info']}")
                    if research_data.get("vertical_match"):
                        summary_parts.append("ターゲット業種")

                from agents.sales.notion_client import map_temperature, map_channel

                page_id = await notion.create_inquiry(
                    company_name=(research_data or {}).get("company_name", company),
                    email=email,
                    subject=tool_input.get("subject", ""),
                    temperature=map_temperature(tool_input.get("temperature", "medium")),
                    channel=map_channel(tool_input.get("channel", "web")),
                    received_at="",
                    company_research=" / ".join(summary_parts) if summary_parts else None,
                )

                # Upsert client if new
                if not existing:
                    try:
                        await notion.upsert_client(
                            (research_data or {}).get("company_name", company)
                        )
                    except Exception:
                        pass

                return json.dumps(
                    {
                        "success": True,
                        "data": {
                            "notion_page_id": page_id,
                            "is_existing_client": existing is not None,
                            "research": research_data,
                        },
                    },
                    ensure_ascii=False,
                )

            elif tool_name == "get_open_inquiries":
                inquiries = pipeline.inquiry_store.get_open(
                    tool_input.get("min_hours_open", 0)
                )
                return json.dumps(
                    {
                        "success": True,
                        "data": [inq.to_dict() for inq in inquiries],
                    },
                    ensure_ascii=False,
                )

            elif tool_name == "check_reply_status":
                inq = pipeline.inquiry_store.get(
                    tool_input.get("inquiry_id", "")
                )
                if not inq:
                    return json.dumps(
                        {"success": False, "error": "Inquiry not found"}
                    )
                from datetime import datetime, timezone
                hours_open = (
                    datetime.now(timezone.utc).timestamp()
                    - datetime.fromisoformat(inq.received_at).timestamp()
                ) / 3600
                return json.dumps(
                    {
                        "success": True,
                        "data": {
                            "replied": inq.status == "replied",
                            "replied_at": inq.replied_at,
                            "replied_by": inq.replied_by,
                            "hours_open": round(hours_open, 1),
                        },
                    },
                    ensure_ascii=False,
                )

            elif tool_name == "get_kpi_summary":
                result = pipeline.get_kpi_summary(
                    tool_input.get("period", "daily")
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "log_feedback":
                result = feedback_detector.log_feedback(
                    tool_input.get("client", "unknown"),
                    tool_input.get("category", "process_improvement"),
                    tool_input.get("content", ""),
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "get_feedback_patterns":
                result = feedback_detector.find_patterns(
                    tool_input.get("min_frequency", 2)
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "draft_reply":
                inq = pipeline.inquiry_store.get(
                    tool_input.get("inquiry_id", "")
                )
                if not inq:
                    return json.dumps(
                        {"success": False, "error": "Inquiry not found"}
                    )

                import anthropic

                client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                prompt = (
                    "あなたはStepAI（AI音声オペレーションSaaS）のCS担当者です。\n"
                    "以下の問い合わせに対する返信ドラフトを日本語で作成してください。\n\n"
                    f"From: {inq.sender_name} ({inq.sender_email})\n"
                    f"Company: {inq.sender_company or '不明'}\n"
                    f"Subject: {inq.subject}\n"
                    f"Body: {inq.body}\n\n"
                    f"トーン: {tool_input.get('tone', 'friendly')}\n"
                )
                ctx = tool_input.get("context")
                if ctx:
                    prompt += f"追加コンテキスト: {ctx}\n"
                prompt += (
                    "\nルール:\n"
                    "- 丁寧だが簡潔に（200文字以内）\n"
                    "- StepAIの製品（Reco）の特徴を自然に含める\n"
                    "- 次のアクション（デモ設定、資料送付等）を提案する\n"
                    "- メールの署名は含めない"
                )

                response = client.messages.create(
                    model=settings.reasoner_model,
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                )
                text_block = next(
                    (b for b in response.content if b.type == "text"), None
                )
                draft = text_block.text if text_block else ""
                return json.dumps(
                    {
                        "success": True,
                        "data": {
                            "draft": draft,
                            "inquiry_subject": inq.subject,
                            "tone": tool_input.get("tone", "friendly"),
                        },
                    },
                    ensure_ascii=False,
                )

            elif tool_name == "list_clients":
                if not notion:
                    return json.dumps({"success": True, "data": []})
                clients = await notion.list_clients(tool_input.get("status"))
                return json.dumps(
                    {"success": True, "data": clients}, ensure_ascii=False
                )

            elif tool_name == "update_client":
                if not notion:
                    return json.dumps({"success": False, "error": "Notion not configured"})
                result = await notion.update_client(
                    tool_input.get("company_name", ""),
                    status=tool_input.get("status"),
                    contact_tool=tool_input.get("contact_tool"),
                    flow_url=tool_input.get("flow_url"),
                    memo=tool_input.get("memo"),
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "do_not_send_check":
                result = do_not_send_check(
                    tool_input.get("company_name", ""),
                    tool_input.get("contact_name"),
                    pipeline.inquiry_store,
                )
                return json.dumps(result, ensure_ascii=False)

            elif tool_name == "tier_classify":
                result = tier_classify(
                    tool_input.get("company_name", ""),
                    tool_input.get("industry"),
                    tool_input.get("estimated_revenue"),
                    tool_input.get("seat_count"),
                )
                return json.dumps(result, ensure_ascii=False)

            else:
                return json.dumps(
                    {"success": False, "error": f"Unknown tool: {tool_name}"}
                )

        except Exception as e:
            logger.exception("Tool dispatch failed: %s", tool_name)
            return json.dumps(
                {"success": False, "error": str(e)}, ensure_ascii=False
            )

    return dispatch_tool


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("セールスくん starting up...")
    settings = get_settings()

    # --- Initialize clients ---
    classifier = Classifier(settings)
    reasoner = Reasoner(settings)

    # Gmail (optional - only if credentials are configured)
    gmail: GmailClient | None = None
    if settings.gmail_client_email and settings.gmail_private_key:
        gmail = GmailClient(settings)
        logger.info("Gmail client initialized (service account)")
    else:
        logger.warning("Gmail not configured - email intake disabled")

    # Notion (optional)
    notion: NotionClient | None = None
    if settings.notion_api_key:
        notion = NotionClient(settings)
        logger.info("Notion client initialized")
    else:
        logger.warning("Notion not configured - CRM features disabled")

    # Feedback
    feedback_store = FeedbackStore(settings.data_dir)
    feedback_detector = FeedbackDetector(settings, feedback_store)

    # Pipeline
    pipeline = Pipeline(settings, classifier, gmail, notion)

    # Wire tool dispatcher into reasoner
    dispatch_fn = await _build_tool_dispatcher(
        settings, pipeline, notion, feedback_detector
    )
    reasoner.set_tool_dispatcher(dispatch_fn)

    # --- Slack app ---
    app = create_slack_app(
        settings, classifier, reasoner, feedback_detector, pipeline
    )

    # Build Slack post function
    from slack_sdk.web.async_client import AsyncWebClient

    slack_client = AsyncWebClient(token=settings.slack_bot_token)

    async def slack_post_fn(channel: str, text: str) -> str | None:
        try:
            result = await slack_client.chat_postMessage(
                channel=channel, text=text
            )
            return result.get("ts")
        except Exception:
            logger.exception("Failed to post Slack message")
            return None

    async def notify_fn(text: str, thread_ts: str | None = None):
        cs_channel = settings.cs_channel_id
        if cs_channel:
            try:
                await slack_client.chat_postMessage(
                    channel=cs_channel, text=text, thread_ts=thread_ts
                )
            except Exception:
                logger.exception("Failed to send notification")

    # --- Scheduler ---
    scheduler = setup_scheduler(
        settings, pipeline, gmail, slack_post_fn, notify_fn
    )
    scheduler.start()
    logger.info("Scheduler started")

    # --- Startup notification ---
    if not settings.shadow_mode:
        await notify_fn("セールスくん起動した。パイプライン監視中。")
    else:
        logger.info("SHADOW_MODE: セールスくん起動（通知なし）")

    # --- Start Slack (blocks) ---
    logger.info("Starting Slack Socket Mode...")
    await start_slack_app(app, settings)


def run():
    """Entry point for pyproject.toml script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("セールスくん shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    run()
