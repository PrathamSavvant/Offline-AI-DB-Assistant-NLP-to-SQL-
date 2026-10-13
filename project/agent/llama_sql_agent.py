"""Bounded local-model SQL composition agent."""
from __future__ import annotations

from typing import Any, Dict, Tuple


class LlamaSQLAgent:
    """Compose SQL from an already-ready deterministic Query Plan."""

    def __init__(self, prompt_builder: Any, llm: Any):
        self.prompt_builder = prompt_builder
        self.llm = llm

    def compose(self, context: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        plan = context.get("query_plan") or {}
        # Empty plans are accepted only for backward-compatible isolated unit
        # integrations. The production application always wires a plan builder.
        if plan and plan.get("status") != "ready":
            raise ValueError("Llama SQL Agent requires a ready Query Plan.")
        prompt = self.prompt_builder.build(context)
        return prompt, self.llm.generate_structured_sql(prompt)

    def explain(self, sql: str) -> str:
        """Produce the existing concise explanation through the same model boundary."""
        return self.llm.explain_sql(sql)
