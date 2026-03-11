"""セールスくん - Feedback detection and logging.

Ports feedback-detect.ts and log-feedback.ts from maakun.
Uses JSON file storage instead of Redis.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import anthropic

from agents.sales.config import Settings

logger = logging.getLogger(__name__)

FeedbackCategory = Literal[
    "positive", "feature_request", "bug", "complaint", "process_improvement"
]

FB_CATEGORIES: list[FeedbackCategory] = [
    "positive",
    "feature_request",
    "bug",
    "complaint",
    "process_improvement",
]


@dataclass
class FeedbackEntry:
    id: str
    client: str
    category: FeedbackCategory
    content: str
    source: str  # "email" | "slack"
    detected_at: str
    related_inquiry_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client": self.client,
            "category": self.category,
            "content": self.content,
            "source": self.source,
            "detected_at": self.detected_at,
            "related_inquiry_id": self.related_inquiry_id,
        }


class FeedbackStore:
    """JSON-based feedback storage."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "feedback.json"
        self._data: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("Failed to load feedback data, starting fresh")
                self._data = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, entry: FeedbackEntry) -> dict[str, Any]:
        """Add a feedback entry. Returns log result with pattern detection."""
        self._data.append(entry.to_dict())
        self._save()

        # Count entries in same category
        same_category = [
            e for e in self._data if e["category"] == entry.category
        ]
        same_count = len(same_category)

        return {
            "id": entry.id,
            "same_category_count": same_count,
            "is_pattern": same_count >= 2,
        }

    def get_patterns(
        self, min_frequency: int = 2
    ) -> list[dict[str, Any]]:
        """Get recurring feedback patterns grouped by category."""
        patterns = []
        for category in FB_CATEGORIES:
            entries = [e for e in self._data if e["category"] == category]
            if len(entries) >= min_frequency:
                patterns.append(
                    {
                        "category": category,
                        "count": len(entries),
                        "entries": [
                            {
                                "id": e["id"],
                                "client": e["client"],
                                "content": e["content"],
                                "detected_at": e["detected_at"],
                            }
                            for e in entries
                        ],
                    }
                )
        return patterns


class FeedbackDetector:
    """Detects and processes feedback from Slack messages."""

    def __init__(self, settings: Settings, store: FeedbackStore) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.classifier_model
        self._store = store
        self._settings = settings

    def detect_feedback(
        self, message_text: str, sender_name: str
    ) -> dict[str, Any] | None:
        """Extract structured feedback from a message. Returns None if no feedback."""
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract client feedback from this Slack message. "
                            "If no client feedback is present, return null.\n\n"
                            f'Message from {sender_name}: "{message_text}"\n\n'
                            "Respond with ONLY valid JSON:\n"
                            '{"client": "<company name>", '
                            '"category": "<positive|feature_request|bug|complaint|process_improvement>", '
                            '"content": "<concise summary>"}\n\n'
                            'Or if no feedback: {"client": null}'
                        ),
                    },
                    {"role": "assistant", "content": "{"},
                ],
            )

            content = response.content[0]
            if content.type != "text":
                return None

            raw = "{" + content.text
            raw = raw.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(raw)

            if not parsed.get("client"):
                return None

            return {
                "client": parsed["client"],
                "category": parsed.get("category", "process_improvement"),
                "content": parsed.get("content", message_text[:200]),
            }

        except Exception:
            logger.exception("Feedback extraction failed")
            return None

    def log_feedback(
        self,
        client: str,
        category: FeedbackCategory,
        content: str,
        source: str = "slack",
    ) -> dict[str, Any]:
        """Store a feedback entry. Returns pattern detection info."""
        entry = FeedbackEntry(
            id=f"fb-{int(time.time())}-{uuid.uuid4().hex[:6]}",
            client=client,
            category=category,
            content=content,
            source=source,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )

        result = self._store.add(entry)

        logger.info(
            "Feedback logged",
            extra={
                "id": result["id"],
                "client": client,
                "category": category,
                "same_category_count": result["same_category_count"],
            },
        )

        return {
            "success": True,
            "data": result,
        }

    def find_patterns(self, min_frequency: int = 2) -> dict[str, Any]:
        """Get recurring feedback patterns."""
        patterns = self._store.get_patterns(min_frequency)
        return {"success": True, "data": patterns}
