"""セールスくん - Inquiry/sales pipeline management.

Ports flows/ from maakun:
  - email-intake.ts → process_email_intake()
  - bottleneck-check.ts → check_bottlenecks()
  - kpi-report.ts → get_kpi_summary()

Also implements tier classification and Do-Not-Send checks from the sales prompt.
Uses JSON file storage for inquiry tracking instead of Redis.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Literal

import anthropic

from agents.sales.classifier import Classifier, ActionType, EmailClassification
from agents.sales.config import Settings
from agents.sales.gmail_client import GmailClient, ParsedEmail, FramerFormData, parse_framer_form
from agents.sales.notion_client import NotionClient, map_temperature, map_channel
from agents.sales.research import research_company

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inquiry data model (JSON storage)
# ---------------------------------------------------------------------------

@dataclass
class Inquiry:
    id: str
    email_id: str
    sender_name: str
    sender_email: str
    sender_domain: str
    sender_company: str | None
    subject: str
    body: str
    received_at: str
    classification_type: str
    classification_temperature: str | None
    classification_confidence: float
    research: dict[str, Any] | None = None
    is_existing_client: bool = False
    status: str = "open"  # open | replied | escalated | closed
    replied_at: str | None = None
    replied_by: str | None = None
    slack_ts: str | None = None
    slack_channel: str | None = None
    channel: str = "web"  # web | linkedin | referral | other
    notion_page_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email_id": self.email_id,
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "sender_domain": self.sender_domain,
            "sender_company": self.sender_company,
            "subject": self.subject,
            "body": self.body,
            "received_at": self.received_at,
            "classification_type": self.classification_type,
            "classification_temperature": self.classification_temperature,
            "classification_confidence": self.classification_confidence,
            "research": self.research,
            "is_existing_client": self.is_existing_client,
            "status": self.status,
            "replied_at": self.replied_at,
            "replied_by": self.replied_by,
            "slack_ts": self.slack_ts,
            "slack_channel": self.slack_channel,
            "channel": self.channel,
            "notion_page_id": self.notion_page_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Inquiry:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Inquiry Store (JSON file)
# ---------------------------------------------------------------------------

class InquiryStore:
    """Persistent inquiry store using JSON files."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "inquiries.json"
        self._processed_path = data_dir / "processed_emails.json"
        self._inquiries: dict[str, dict[str, Any]] = {}
        self._processed: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._inquiries = data if isinstance(data, dict) else {}
            except Exception:
                logger.warning("Failed to load inquiries, starting fresh")
                self._inquiries = {}

        if self._processed_path.exists():
            try:
                data = json.loads(self._processed_path.read_text(encoding="utf-8"))
                self._processed = set(data) if isinstance(data, list) else set()
            except Exception:
                self._processed = set()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._inquiries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_processed(self) -> None:
        self._processed_path.parent.mkdir(parents=True, exist_ok=True)
        self._processed_path.write_text(
            json.dumps(list(self._processed), ensure_ascii=False),
            encoding="utf-8",
        )

    def is_processed(self, email_id: str) -> bool:
        return email_id in self._processed

    def mark_processed(self, email_id: str) -> None:
        self._processed.add(email_id)
        self._save_processed()

    def create(self, inquiry: Inquiry) -> None:
        self._inquiries[inquiry.id] = inquiry.to_dict()
        self._save()

    def get(self, inquiry_id: str) -> Inquiry | None:
        data = self._inquiries.get(inquiry_id)
        if data:
            return Inquiry.from_dict(data)
        return None

    def get_open(self, min_hours_open: float = 0) -> list[Inquiry]:
        now_ts = time.time()
        results = []
        for data in self._inquiries.values():
            if data.get("status") != "open":
                continue
            received = datetime.fromisoformat(data["received_at"]).timestamp()
            hours_open = (now_ts - received) / 3600
            if hours_open >= min_hours_open:
                results.append(Inquiry.from_dict(data))
        return results

    def get_mtd(self) -> list[Inquiry]:
        """Get all inquiries for the current month."""
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        results = []
        for data in self._inquiries.values():
            received = datetime.fromisoformat(data["received_at"])
            if received >= month_start:
                results.append(Inquiry.from_dict(data))
        return results

    def mark_replied(self, inquiry_id: str, replied_by: str) -> None:
        if inquiry_id in self._inquiries:
            self._inquiries[inquiry_id]["status"] = "replied"
            self._inquiries[inquiry_id]["replied_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
            self._inquiries[inquiry_id]["replied_by"] = replied_by
            self._save()

    def mark_escalated(self, inquiry_id: str) -> None:
        if inquiry_id in self._inquiries:
            self._inquiries[inquiry_id]["status"] = "escalated"
            self._save()

    def update_slack_info(
        self, inquiry_id: str, slack_ts: str, slack_channel: str
    ) -> None:
        if inquiry_id in self._inquiries:
            self._inquiries[inquiry_id]["slack_ts"] = slack_ts
            self._inquiries[inquiry_id]["slack_channel"] = slack_channel
            self._save()


# ---------------------------------------------------------------------------
# KPI Summary
# ---------------------------------------------------------------------------

@dataclass
class KPISummary:
    period: str
    mtd_inquiries: int
    monthly_target: int
    progress_rate: float
    remaining_days: int
    required_pace: float
    weekly_count: int
    previous_week_count: int
    week_over_week_change: float
    by_channel: dict[str, int]
    avg_reply_time_hours: float
    unreplied_72h: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "mtd_inquiries": self.mtd_inquiries,
            "monthly_target": self.monthly_target,
            "progress_rate": self.progress_rate,
            "remaining_days": self.remaining_days,
            "required_pace": self.required_pace,
            "weekly_count": self.weekly_count,
            "previous_week_count": self.previous_week_count,
            "week_over_week_change": self.week_over_week_change,
            "by_channel": self.by_channel,
            "avg_reply_time_hours": self.avg_reply_time_hours,
            "unreplied_72h": self.unreplied_72h,
        }


# ---------------------------------------------------------------------------
# Tier Classification
# ---------------------------------------------------------------------------

# Named accounts that always require approval (Tier1)
TIER1_NAMED_ACCOUNTS = {
    "aiful", "アイフル",
    "東京海上", "東京海上hd",
    "ベルシステム24",
    "gmo connect", "gmoコネクト",
    "トランスコスモス",
    "nttビジネスソリューションズ", "ntt",
}


def tier_classify(
    company_name: str,
    industry: str | None = None,
    estimated_revenue: str | None = None,
    seat_count: int | None = None,
) -> dict[str, Any]:
    """Classify company into Tier1/2/3.

    Tier1: Enterprise (egao approval required)
    Tier2: Mid-market (autonomous)
    Tier3: BPO (fast close)
    """
    name_lower = company_name.lower()

    # Check named accounts
    for account in TIER1_NAMED_ACCOUNTS:
        if account in name_lower:
            return {
                "tier": 1,
                "reason": f"名前付きアカウント: {company_name}",
                "requires_approval": True,
            }

    # Check seat count
    if seat_count and seat_count >= 100:
        return {
            "tier": 1,
            "reason": f"コールセンター{seat_count}席以上",
            "requires_approval": True,
        }

    # Check industry for Tier1
    if industry:
        industry_lower = industry.lower()
        tier1_industries = ["銀行", "保険", "証券", "金融"]
        for ind in tier1_industries:
            if ind in industry_lower:
                return {
                    "tier": 1,
                    "reason": f"金融大手: {industry}",
                    "requires_approval": True,
                }

    # Revenue-based classification
    if estimated_revenue:
        try:
            rev_str = re.sub(r"[¥,k万]", "", estimated_revenue.lower())
            rev = float(rev_str)
            # Normalize to monthly yen
            if "万" in estimated_revenue:
                rev *= 10000
            elif "k" in estimated_revenue.lower():
                rev *= 1000

            if rev >= 1_000_000:
                return {
                    "tier": 1,
                    "reason": f"月額¥1M+ポテンシャル",
                    "requires_approval": True,
                }
            elif rev >= 300_000:
                return {
                    "tier": 2,
                    "reason": f"ミドルマーケット ¥300k-¥1M/月",
                    "requires_approval": False,
                }
        except (ValueError, TypeError):
            pass

    # Check for BPO (Tier3)
    if industry:
        bpo_keywords = ["bpo", "アウトソーシング", "委託"]
        for kw in bpo_keywords:
            if kw in industry.lower():
                return {
                    "tier": 3,
                    "reason": "BPOクライアント",
                    "requires_approval": False,
                }

    # Seat count for Tier2
    if seat_count and 20 <= seat_count < 100:
        return {
            "tier": 2,
            "reason": f"コールセンター{seat_count}席（中規模）",
            "requires_approval": False,
        }

    # Default to Tier3
    return {
        "tier": 3,
        "reason": "標準分類",
        "requires_approval": False,
    }


# ---------------------------------------------------------------------------
# Do-Not-Send Check
# ---------------------------------------------------------------------------

def do_not_send_check(
    company_name: str,
    contact_name: str | None = None,
    inquiry_store: InquiryStore | None = None,
) -> dict[str, Any]:
    """Run Do-Not-Send safety checks before outreach.

    Returns: {"allowed": True/False, "reason": str, "checks": list}
    """
    checks: list[dict[str, str]] = []

    # 1. Already approached in last 30 days?
    if inquiry_store:
        now = time.time()
        thirty_days_ago = now - (30 * 86400)
        for inq_data in inquiry_store._inquiries.values():
            if company_name.lower() in (
                inq_data.get("sender_company", "") or ""
            ).lower():
                received = datetime.fromisoformat(
                    inq_data["received_at"]
                ).timestamp()
                if received > thirty_days_ago:
                    checks.append({
                        "check": "already_approached",
                        "result": "BLOCKED",
                        "detail": f"過去30日以内にコンタクト済み (ID: {inq_data['id']})",
                    })
                    return {
                        "allowed": False,
                        "reason": "already_approached",
                        "detail": f"過去30日以内にコンタクト済み",
                        "checks": checks,
                    }

    checks.append({"check": "already_approached", "result": "OK", "detail": ""})

    # 2-6: These checks would require more context (CRM data, egao's calendar, etc.)
    # For now, flag them as manual-check items
    manual_checks = [
        ("egao_direct_contact", "えがおが直接やり取り中か？"),
        ("support_open", "サポート対応中か？"),
        ("recently_churned", "過去90日以内に解約/ロストしたか？"),
        ("duplicate_company", "同じ会社の別担当に送信済みか？"),
        ("bad_timing", "ネガティブニュースが出ていないか？"),
    ]

    for check_name, description in manual_checks:
        checks.append({
            "check": check_name,
            "result": "MANUAL_CHECK",
            "detail": description,
        })

    return {
        "allowed": True,
        "reason": "all_automated_checks_passed",
        "detail": "自動チェックOK。手動確認推奨項目あり。",
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Pipeline Manager
# ---------------------------------------------------------------------------

class Pipeline:
    """Manages the inquiry/sales pipeline."""

    def __init__(
        self,
        settings: Settings,
        classifier: Classifier,
        gmail: GmailClient | None = None,
        notion: NotionClient | None = None,
    ) -> None:
        self._settings = settings
        self._classifier = classifier
        self._gmail = gmail
        self._notion = notion
        self._store = InquiryStore(settings.data_dir)
        self._anthropic = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @property
    def inquiry_store(self) -> InquiryStore:
        return self._store

    # --- Flow A: Email Intake ---

    async def process_email_intake(
        self,
        email: ParsedEmail,
        slack_post_fn=None,
    ) -> Inquiry | None:
        """Email -> classify -> research -> Notion -> Slack notify."""
        cs_channel = self._settings.cs_channel_id
        if not cs_channel:
            logger.error("CS_CHANNEL_ID not configured")
            return None

        # Skip if already processed
        if self._store.is_processed(email.id):
            logger.info("Skipping already-processed email: %s", email.id)
            return None

        logger.info(
            "Processing email intake",
            extra={"email_id": email.id, "from": email.from_email, "subject": email.subject},
        )

        # Step 1: Classify
        classification = self._classifier.classify_email(
            email.subject, email.body, email.from_email
        )

        # Skip high-confidence spam
        if (
            classification.action_type == ActionType.SPAM_EMAIL
            and classification.confidence >= 0.8
        ):
            logger.info("Skipping spam email: %s", email.id)
            self._store.mark_processed(email.id)
            return None

        # Step 2: Parse Framer form data
        framer_data = parse_framer_form(email.html_body)

        contact_email = (framer_data.email if framer_data else None) or email.from_email
        contact_name = (framer_data.name if framer_data else None) or email.from_name
        company_name = (framer_data.company if framer_data else None) or contact_name
        domain = contact_email.split("@")[1] if "@" in contact_email else ""

        # Step 3: Research company
        research_result = await research_company(company_name, domain, self._settings)
        research_data = (
            research_result.get("data") if research_result.get("success") else None
        )

        # Step 4: Check existing client
        is_existing = False
        if self._notion:
            client_record = await self._notion.check_client(
                research_data.get("company_name", company_name) if research_data else company_name
            )
            is_existing = client_record is not None

        # Step 5: Determine channel
        channel = "web"
        if re.search(r"linkedin", email.body, re.IGNORECASE):
            channel = "linkedin"
        elif re.search(r"紹介|ご紹介|referred", email.body, re.IGNORECASE):
            channel = "referral"

        # Step 6: Create Notion inquiry
        notion_page_id = None
        if self._notion:
            research_summary = None
            if research_data:
                parts = []
                if research_data.get("industry"):
                    parts.append(f"業種: {research_data['industry']}")
                if research_data.get("size"):
                    parts.append(f"規模: {research_data['size']}")
                if research_data.get("location"):
                    parts.append(f"所在地: {research_data['location']}")
                if research_data.get("call_center_info"):
                    parts.append(f"CC: {research_data['call_center_info']}")
                if research_data.get("vertical_match"):
                    parts.append("ターゲット業種")
                research_summary = " / ".join(parts) if parts else None

            notion_page_id = await self._notion.create_inquiry(
                company_name=research_data.get("company_name", company_name) if research_data else company_name,
                email=contact_email,
                subject=email.subject,
                temperature=map_temperature(classification.temperature),
                channel=map_channel(channel),
                received_at=email.received_at,
                company_research=research_summary,
            )

        # Step 7: Build body text from framer data if available
        body = email.body
        if framer_data:
            parts = []
            if framer_data.requirement:
                parts.append(f"要件: {framer_data.requirement}")
            if framer_data.message:
                parts.append(f"メッセージ: {framer_data.message}")
            if framer_data.referral_source:
                parts.append(f"きっかけ: {framer_data.referral_source}")
            if parts:
                body = "\n".join(parts)

        # Step 8: Create local inquiry record
        inquiry = Inquiry(
            id=email.id,
            email_id=email.id,
            sender_name=contact_name,
            sender_email=contact_email,
            sender_domain=domain,
            sender_company=research_data.get("company_name", company_name) if research_data else company_name,
            subject=email.subject,
            body=body,
            received_at=email.received_at,
            classification_type=classification.action_type.value,
            classification_temperature=classification.temperature,
            classification_confidence=classification.confidence,
            research=research_data,
            is_existing_client=is_existing,
            channel=channel,
            notion_page_id=notion_page_id,
        )
        self._store.create(inquiry)
        self._store.mark_processed(email.id)

        # Step 9: Post to Slack
        if slack_post_fn:
            vertical_tag = " \U0001f3af" if (research_data or {}).get("vertical_match") else ""
            crm_label = "既存" if is_existing else "新規（過去接点なし）"
            temp_emoji = (
                "\U0001f525" if classification.temperature == "high"
                else "\U0001f7e1" if classification.temperature == "medium"
                else ""
            )

            research_line = "🏢 リサーチ情報なし"
            if research_data:
                parts = [research_data.get("industry", "不明")]
                parts.append(research_data.get("size", "規模不明"))
                parts.append(research_data.get("location", "所在地不明"))
                if research_data.get("call_center_info"):
                    parts.append(research_data["call_center_info"])
                research_line = "🏢 " + " / ".join(parts)

            text = (
                f"📩 新規問い合わせ {temp_emoji}\n\n"
                f"*From:* {contact_name} ({contact_email})\n"
                f"*Company:* {inquiry.sender_company or '不明'}\n"
                f"{research_line}\n"
                f"*Subject:* {email.subject}\n"
                f"*Summary:* {body[:200]}{'...' if len(body) > 200 else ''}\n"
                f"*CRM:* {crm_label}\n"
                f"*Vertical:* {(research_data or {}).get('industry', '不明')}{vertical_tag}\n\n"
                f"対応する？返信ドラフト作る？"
            )

            if not self._settings.shadow_mode:
                slack_ts = await slack_post_fn(cs_channel, text)
                if slack_ts:
                    self._store.update_slack_info(inquiry.id, slack_ts, cs_channel)
                    # Update Notion with Slack link
                    if notion_page_id and self._notion:
                        domain = self._settings.slack_workspace_domain
                        slack_url = (
                            f"https://{domain}.slack.com/archives/"
                            f"{cs_channel}/p{slack_ts.replace('.', '')}"
                        )
                        await self._notion.update_inquiry_slack_url(
                            notion_page_id, slack_url
                        )
            else:
                logger.info(
                    "SHADOW_MODE: Would post inquiry notification",
                    extra={"channel": cs_channel, "inquiry_id": inquiry.id},
                )

        # Step 10: Upsert client if new
        if not is_existing and self._notion:
            try:
                await self._notion.upsert_client(
                    research_data.get("company_name", company_name) if research_data else company_name
                )
            except Exception:
                logger.warning("Failed to upsert new client")

        logger.info(
            "Email intake complete",
            extra={
                "email_id": email.id,
                "classification": classification.action_type.value,
                "vertical_match": (research_data or {}).get("vertical_match"),
                "notion_page_id": notion_page_id,
            },
        )
        return inquiry

    # --- Flow B: Bottleneck Check ---

    async def check_bottlenecks(
        self,
        slack_post_fn=None,
    ) -> dict[str, Any]:
        """Find overdue inquiries (24h/48h/72h escalation)."""
        cs_channel = self._settings.cs_channel_id
        if not cs_channel:
            logger.error("CS_CHANNEL_ID not configured")
            return {"red": [], "orange": [], "green": []}

        logger.info("Running bottleneck check")
        open_inquiries = self._store.get_open()

        if not open_inquiries:
            logger.info("No open inquiries - bottleneck check clean")
            return {"red": [], "orange": [], "green": []}

        now = time.time()
        red: list[dict[str, str]] = []
        orange: list[dict[str, str]] = []
        green: list[dict[str, str]] = []

        for inquiry in open_inquiries:
            # Check Gmail for actual reply
            if self._gmail and inquiry.email_id:
                reply_check = self._gmail.check_reply_status(
                    inquiry.email_id, inquiry.received_at
                )
                if reply_check.get("replied"):
                    self._store.mark_replied(inquiry.id, "auto-detected")
                    green.append({
                        "company": inquiry.sender_company or inquiry.sender_name,
                        "handled_by": "auto-detected",
                    })
                    continue

            received_ts = datetime.fromisoformat(inquiry.received_at).timestamp()
            hours_open = (now - received_ts) / 3600

            if hours_open >= 72:
                red.append({
                    "company": inquiry.sender_company or inquiry.sender_name,
                    "contact": inquiry.sender_name,
                    "received_date": inquiry.received_at[5:10].replace("-", "/"),
                    "subject": inquiry.subject,
                    "id": inquiry.id,
                })
            elif hours_open >= 48:
                orange.append({
                    "company": inquiry.sender_company or inquiry.sender_name,
                    "subject": inquiry.subject,
                })

        # Post report
        if slack_post_fn:
            lines = ["⏰ 対応状況レポート"]

            if red:
                lines.append(f"\n🔴 未返信 3日以上: {len(red)}件")
                for item in red:
                    lines.append(
                        f"  - {item['company']} ({item['contact']}) "
                        f"— {item['received_date']}受信、{item['subject']}"
                    )

            if orange:
                lines.append(f"\n🟠 未返信 2日: {len(orange)}件")
                for item in orange:
                    lines.append(f"  - {item['company']} — {item['subject']}")

            if green:
                lines.append(f"\n🟢 対応済み（昨日）: {len(green)}件")
                for item in green:
                    lines.append(f"  - {item['company']} ✅ {item['handled_by']}対応")

            if not red and not orange:
                lines.append("\n✅ 未返信案件なし")

            text = "\n".join(lines)

            if not self._settings.shadow_mode:
                await slack_post_fn(cs_channel, text)
            else:
                logger.info("SHADOW_MODE: Would post bottleneck report")

            # Escalate 72h+ items
            escalation_user = self._settings.escalation_user_id
            for item in red:
                self._store.mark_escalated(item["id"])
                if escalation_user and not self._settings.shadow_mode:
                    await slack_post_fn(
                        cs_channel,
                        f"⚠️ <@{escalation_user}> "
                        f"{item['company']}（{item['contact']}）、3日未返信。対応漏れの可能性。",
                    )

        logger.info(
            "Bottleneck check complete",
            extra={"red": len(red), "orange": len(orange), "green": len(green)},
        )
        return {"red": red, "orange": orange, "green": green}

    # --- Flow C: KPI Report ---

    def get_kpi_summary(
        self, period: str = "daily"
    ) -> dict[str, Any]:
        """Calculate KPI summary."""
        now = datetime.now(timezone.utc)
        mtd_inquiries = self._store.get_mtd()
        mtd_count = len(mtd_inquiries)

        # Channel breakdown
        by_channel: dict[str, int] = {}
        for inq in mtd_inquiries:
            ch = inq.channel or "other"
            by_channel[ch] = by_channel.get(ch, 0) + 1

        # Average reply time
        replied = [
            inq for inq in mtd_inquiries
            if inq.status == "replied" and inq.replied_at
        ]
        avg_reply_hours = 0.0
        if replied:
            total_hours = sum(
                (
                    datetime.fromisoformat(inq.replied_at).timestamp()
                    - datetime.fromisoformat(inq.received_at).timestamp()
                )
                / 3600
                for inq in replied
            )
            avg_reply_hours = round(total_hours / len(replied), 1)

        # 72h+ unreplied
        unreplied_72h = len([
            inq for inq in mtd_inquiries
            if inq.status == "open"
            and (now.timestamp() - datetime.fromisoformat(inq.received_at).timestamp()) / 3600 >= 72
        ])

        # Weekly counts
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        prev_week_start = week_start - timedelta(days=7)

        weekly_count = sum(
            1 for inq in mtd_inquiries
            if datetime.fromisoformat(inq.received_at) >= week_start
        )
        prev_week_count = sum(
            1 for inq in mtd_inquiries
            if prev_week_start <= datetime.fromisoformat(inq.received_at) < week_start
        )

        # Remaining days
        import calendar as cal
        last_day = cal.monthrange(now.year, now.month)[1]
        remaining_days = last_day - now.day

        target = self._settings.kpi_monthly_target
        remaining_needed = target - mtd_count
        required_pace = round(remaining_needed / remaining_days, 1) if remaining_days > 0 else 0

        wow_change = (
            round((weekly_count - prev_week_count) / prev_week_count * 100)
            if prev_week_count > 0
            else 0
        )

        summary = KPISummary(
            period=period,
            mtd_inquiries=mtd_count,
            monthly_target=target,
            progress_rate=round(mtd_count / target * 100) if target > 0 else 0,
            remaining_days=remaining_days,
            required_pace=max(0, required_pace),
            weekly_count=weekly_count,
            previous_week_count=prev_week_count,
            week_over_week_change=wow_change,
            by_channel=by_channel,
            avg_reply_time_hours=avg_reply_hours,
            unreplied_72h=unreplied_72h,
        )

        logger.info(
            "KPI summary calculated",
            extra={
                "period": period,
                "mtd": mtd_count,
                "progress": summary.progress_rate,
            },
        )

        return {"success": True, "data": summary.to_dict()}

    async def post_kpi_report(self, slack_post_fn=None) -> None:
        """Generate and post KPI report with trend analysis."""
        cs_channel = self._settings.cs_channel_id
        if not cs_channel:
            logger.error("CS_CHANNEL_ID not configured")
            return

        kpi_result = self.get_kpi_summary("daily")
        if not kpi_result.get("success") or not kpi_result.get("data"):
            logger.error("Failed to get KPI summary")
            return

        kpi = kpi_result["data"]

        # Generate trend analysis with Sonnet
        trend_analysis = ""
        try:
            prompt = (
                "あなたはStepAIのCSオペレーションアナリストです。\n"
                "以下のKPIデータから、2-3行のトレンド分析と1つの改善提案を日本語で出してください。\n\n"
                f"KPIデータ:\n"
                f"- MTD流入: {kpi['mtd_inquiries']}件 / 月目標{kpi['monthly_target']}件"
                f"（進捗率 {kpi['progress_rate']}%）\n"
                f"- 今週: {kpi['weekly_count']}件（先週: {kpi['previous_week_count']}件、"
                f"{'+' if kpi['week_over_week_change'] >= 0 else ''}{kpi['week_over_week_change']}%）\n"
                f"- チャネル別: {json.dumps(kpi['by_channel'], ensure_ascii=False)}\n"
                f"- 平均返信時間: {kpi['avg_reply_time_hours']}時間\n"
                f"- 72h超え未返信: {kpi['unreplied_72h']}件\n"
                f"- 残り日数: {kpi['remaining_days']}日\n\n"
                "ルール:\n"
                "- 箇条書きで簡潔に（各1行以内）\n"
                "- 「〜かも」「〜を確認した方がいい？」の仮説提案スタイル\n"
                "- ネガティブな場合も改善アクションとセットで"
            )

            response = self._anthropic.messages.create(
                model=self._settings.reasoner_model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )
            text_block = next(
                (b for b in response.content if b.type == "text"), None
            )
            trend_analysis = text_block.text if text_block else ""
        except Exception:
            logger.warning("Trend analysis generation failed")

        # Format report
        now = datetime.now(timezone.utc)
        date_str = f"{now.month}/{now.day}"
        channel_breakdown = " / ".join(
            f"{ch} {count}" for ch, count in kpi["by_channel"].items()
        )
        wow_sign = "+" if kpi["week_over_week_change"] >= 0 else ""

        lines = [
            f"📈 流入KPIレポート（{date_str}）",
            "",
            f"*MTD:* {kpi['mtd_inquiries']}件 / 月目標{kpi['monthly_target']}件"
            f"（進捗率 {kpi['progress_rate']}%）",
            f"*残り日数:* {kpi['remaining_days']}日 → 必要ペース: {kpi['required_pace']}件/日",
            f"*今週:* {kpi['weekly_count']}件（先週: {kpi['previous_week_count']}件、"
            f"{wow_sign}{kpi['week_over_week_change']}%）",
        ]
        if channel_breakdown:
            lines.append(f"*チャネル別:* {channel_breakdown}")

        if kpi["unreplied_72h"] > 0:
            lines.append(f"\n⚠️ 72時間超え未返信: {kpi['unreplied_72h']}件")

        if trend_analysis:
            lines.extend(["", "📊 トレンド:", trend_analysis])

        text = "\n".join(lines)

        if slack_post_fn:
            if not self._settings.shadow_mode:
                await slack_post_fn(cs_channel, text)
            else:
                logger.info("SHADOW_MODE: Would post KPI report")

        logger.info(
            "KPI report complete",
            extra={"mtd": kpi["mtd_inquiries"], "progress": kpi["progress_rate"]},
        )
