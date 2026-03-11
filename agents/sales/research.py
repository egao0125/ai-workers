"""セールスくん - Company research.

Ports research-company.ts from maakun. Fetches company website and extracts
industry, size, location, call center info, vertical match.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from agents.sales.config import Settings

logger = logging.getLogger(__name__)

# Target verticals for Reco
TARGET_VERTICALS = [
    "債権回収", "保険", "銀行", "金融", "コールセンター", "BPO", "通信",
]


@dataclass
class CompanyResearch:
    company_name: str
    industry: str = "不明"
    size: str | None = None
    location: str | None = None
    call_center_info: str | None = None
    vertical_match: bool = False
    recent_news: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "industry": self.industry,
            "size": self.size,
            "location": self.location,
            "call_center_info": self.call_center_info,
            "vertical_match": self.vertical_match,
            "recent_news": self.recent_news,
        }

    def summary_line(self) -> str:
        """Build a one-line summary for Notion."""
        parts = []
        if self.industry:
            parts.append(f"業種: {self.industry}")
        if self.size:
            parts.append(f"規模: {self.size}")
        if self.location:
            parts.append(f"所在地: {self.location}")
        if self.call_center_info:
            parts.append(f"CC: {self.call_center_info}")
        if self.vertical_match:
            parts.append("ターゲット業種")
        return " / ".join(parts)


async def _fetch_url(url: str, timeout: float = 10.0) -> str:
    """Fetch URL content as text. Returns empty string on failure."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "StepAI-Research/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            # Strip HTML tags for analysis
            text = re.sub(r"<[^>]+>", " ", resp.text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:5000]
    except Exception:
        logger.debug("Failed to fetch URL: %s", url)
        return ""


async def research_company(
    company_name: str,
    email_domain: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Research a company by name/domain.

    Returns: {"success": True/False, "data": CompanyResearch dict or None}
    """
    if not company_name and not email_domain:
        return {"success": False, "error": "Company name or email domain is required"}

    try:
        research = CompanyResearch(company_name=company_name)

        # Try fetching the company website
        site_text = ""
        if email_domain:
            domain = email_domain.lstrip("@")
            site_text = await _fetch_url(f"https://{domain}")

        if site_text:
            # Check for vertical match
            for vertical in TARGET_VERTICALS:
                if vertical in site_text:
                    research.vertical_match = True
                    research.industry = vertical
                    break

            # Extract location
            location_match = re.search(
                r"(?:所在地|住所|本社)[：:\s]*([^\n]{5,50})", site_text
            )
            if location_match:
                research.location = location_match.group(1).strip()

            # Extract employee count
            size_match = re.search(
                r"(?:従業員|社員)[数：:\s]*[約]?(\d[\d,]*)", site_text
            )
            if size_match:
                research.size = size_match.group(1).replace(",", "") + "名"

            # Check for call center info
            if re.search(
                r"コールセンター|カスタマーセンター|オペレーター|架電|受電",
                site_text,
            ):
                research.call_center_info = "コールセンター運用あり"

        logger.info(
            "Company researched",
            extra={
                "company": company_name,
                "domain": email_domain,
                "vertical_match": research.vertical_match,
                "has_website": len(site_text) > 0,
            },
        )

        return {"success": True, "data": research.to_dict()}

    except Exception:
        logger.exception("Company research failed: %s", company_name)
        return {
            "success": True,
            "data": CompanyResearch(company_name=company_name).to_dict(),
        }
