"""Search persistent table knowledge without rescanning or reinterpreting schema."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger(__name__)


def _words(value: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9]+", str(value).replace("_", " ").lower()))


class KnowledgeSearchEngine:
    """Ranks stored profiles using their business-facing fields only."""

    def __init__(self, profiles: Dict[str, Dict[str, Any]]):
        self.profiles = profiles or {}

    @staticmethod
    def _text(values: Any) -> str:
        if isinstance(values, dict):
            return " ".join(KnowledgeSearchEngine._text(value) for value in values.values())
        if isinstance(values, list):
            return " ".join(KnowledgeSearchEngine._text(value) for value in values)
        return str(values or "")

    def search(self, question: str, limit: int = 12) -> List[Dict[str, Any]]:
        query = _words(question)
        ranked = []
        for table, profile in self.profiles.items():
            fields = {
                "aliases": profile.get("aliases", []), "purpose": [profile.get("purpose", ""), profile.get("description", "")],
                "business_columns": profile.get("business_columns", []), "abbreviations": profile.get("abbreviations", {}),
                "common_queries": profile.get("common_queries", []), "business_name": profile.get("business_name", ""),
            }
            matched, score = [], 0
            for field, value in fields.items():
                overlap = query & _words(self._text(value))
                if overlap:
                    weight = {"aliases": 8, "abbreviations": 7, "business_name": 6, "purpose": 3, "business_columns": 3, "common_queries": 2}[field]
                    score += len(overlap) * weight
                    matched.append(f"{field}: {', '.join(sorted(overlap))}")
            if score:
                ranked.append({"table": table, "profile": profile, "score": score, "matches": matched})
        result = sorted(ranked, key=lambda item: (-item["score"], item["table"]))[:max(1, limit)]
        logger.info("Knowledge search matched %d profile(s), returning %d", len(ranked), len(result))
        return result
