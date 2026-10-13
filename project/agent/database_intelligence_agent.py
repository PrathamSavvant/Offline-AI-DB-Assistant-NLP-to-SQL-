"""Deterministic database retrieval and planning agent.

This agent contains no model calls.  It coordinates the existing cached schema,
knowledge, retrieval, predicate, intent and Query Plan components.
"""
from __future__ import annotations

from typing import Any, Dict

from services.sql_reasoning_engine import QueryContextBuilder


class DatabaseIntelligenceAgent(QueryContextBuilder):
    """First-class deterministic boundary that produces one Query Plan."""

    def build(self, question: str) -> Dict[str, Any]:
        context = super().build(question)
        # Defense-in-depth: pending stores are not constructor inputs, and any
        # accidentally attached diagnostic data is removed at this boundary.
        for key in ("pending_knowledge", "_pending_knowledge", "pending_dictionary"):
            context.pop(key, None)
        context["knowledge_matches"] = [
            item for item in context.get("knowledge_matches", [])
            if str(item.get("source", "")).lower() not in {"pending", "pending_knowledge"}
        ]
        if self.query_plan_builder is not None:
            context["query_plan"] = self.query_plan_builder.build(context).to_dict()
        return context

    def plan(self, question: str) -> Dict[str, Any]:
        return self.build(question)
