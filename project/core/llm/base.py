"""Minimal provider contract used by the SQL reasoning layer."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseLLM(ABC):
    """Provider-neutral interface; implementations own HTTP/API differences."""

    @abstractmethod
    def health_check(self) -> None:
        """Raise RuntimeError when the configured model service is unavailable."""

    @abstractmethod
    def generate_structured_sql(self, prompt: str) -> Dict[str, Any]:
        """Return the existing SQL-decision contract used by SQLReasoningEngine."""

    @abstractmethod
    def explain_sql(self, sql: str) -> str:
        """Return a concise natural-language explanation of SQL."""
