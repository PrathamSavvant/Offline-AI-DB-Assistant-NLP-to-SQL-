"""Pending table metadata and table-scoped business-term review storage."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)


class PendingKnowledgeRepository:
    def __init__(self, path: Optional[Path] = None):
        self._custom_path = path
        self._data: Dict[str, Any] = {"version": 3, "business_terms": {}, "table_contexts": {}, "dismissed_tables": [], "feedback_recommendations": [], "query_diagnostics": []}
        self._load()

    @property
    def path(self) -> Path:
        resolved = self._custom_path or db_context.get_knowledge_dir() / "pending_knowledge.json"
        return resolved

    def _load(self) -> None:
        changed = False
        p = self.path
        logger.debug(
            "[KNOWLEDGE] PendingKnowledge namespace=%s exists=%s",
            p.parent.name, p.exists(),
        )
        if p.exists():
            with open(p, "r", encoding="utf-8") as handle:
                self._data = json.load(handle)
        self._data["version"] = 3
        self._data.setdefault("business_terms", {})
        self._data.setdefault("table_contexts", {})
        self._data.setdefault("dismissed_tables", [])
        self._data.setdefault("feedback_recommendations", [])
        self._data.setdefault("query_diagnostics", [])
        # v3 pending records used an optional suggested_column. Pending term
        # review is intentionally table-scoped business vocabulary, so remove
        # that obsolete field during load rather than merely hiding it.
        for item in self._data["business_terms"].values():
            if "suggested_column" in item:
                item.pop("suggested_column", None); changed = True
            if item.get("status") != "pending":
                item["status"] = "pending"; changed = True
        if changed:
            self._save()

    def reload(self) -> None:
        self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, ensure_ascii=False)
        temporary.replace(self.path)

    def migrate(self, legacy_pending: Dict[str, Dict[str, Any]]) -> None:
        changed = False
        for token, item in legacy_pending.items():
            if token not in self._data["business_terms"]:
                self._data["business_terms"][token] = {"detected_term": token, "table": item.get("table", ""), "suggested_meaning": "", "context": "", "status": "pending"}
                changed = True
        if changed:
            self._save()

    def pending_tables(
        self, schema: Dict[str, Any], defined: Any,
        relationships: Optional[Dict[str, Any]] = None,
        importance: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Return pending tables ordered for human review, never for RAG."""
        terms_by_table: Dict[str, List[str]] = {}
        for item in self.business_terms():
            if item.get("table"):
                terms_by_table.setdefault(item["table"], []).append(item["detected_term"])
        relationship_rows = (relationships or {}).get("relationships", [])
        incoming: Dict[str, int] = {}
        outgoing: Dict[str, int] = {}
        for relationship in relationship_rows:
            outgoing[relationship.get("from_table", "")] = outgoing.get(relationship.get("from_table", ""), 0) + 1
            incoming[relationship.get("to_table", "")] = incoming.get(relationship.get("to_table", ""), 0) + 1
        importance_by_table = {
            item.get("table"): item for item in (importance or {}).get("rankings", [])
        }
        result = []
        for table in schema.get("tables", []):
            name = table["name"]
            knowledge = defined.table(name)
            tokens = sorted(terms_by_table.get(name, []), key=str.lower)
            # A defined table normally leaves the table-review queue, but it
            # remains here while it owns unresolved business tokens. Otherwise
            # the highest-value token queues disappear merely because a user
            # supplied a definition first.
            if not defined.is_defined(knowledge) or tokens:
                row_count = int(table.get("approx_row_count") or 0)
                in_count, out_count = incoming.get(name, 0), outgoing.get(name, 0)
                ranking = importance_by_table.get(name, {})
                # Schema crawler's normalized importance is trusted when it
                # exists; relation counts and cached row count remain visible
                # so the ordering is explainable and deterministic.
                importance_score = float(ranking.get("score") or 0)
                priority_score = round(importance_score * 1000 + in_count * 20 + out_count * 12, 3)
                result.append({
                    "table": name, "missing": ["definition", "synonyms", "abbreviations"],
                    "approx_row_count": row_count,
                    "incoming_relationship_count": in_count,
                    "outgoing_relationship_count": out_count,
                    "priority_score": priority_score,
                    "unidentified_token_count": len(tokens),
                    "unidentified_tokens": tokens,
                })
        # Pending tokens are the explicit review queue: the table with the
        # most unresolved tokens must always come first. Table name makes only
        # equal-count rows deterministic; priority remains explanatory data.
        return sorted(result, key=lambda row: (-row["unidentified_token_count"], row["table"].lower()))

    def business_terms(self) -> List[Dict[str, Any]]:
        """Return the pending-review contract; physical columns are excluded."""
        return [{"id": key, "detected_term": value.get("detected_term", key), "table": value.get("table", ""), "suggested_meaning": value.get("suggested_meaning", ""), "status": "pending"} for key, value in sorted(self._data["business_terms"].items())]

    def table_context(self, table: str) -> str:
        return str(self._data["table_contexts"].get(table, ""))

    def save_table_context(self, table: str, context: str) -> str:
        self._data["table_contexts"][table] = str(context or "").strip()
        self._save()
        return self.table_context(table)

    def accept_table_context(
        self, table: str, defined: Any, inline_context: Optional[str] = None
    ) -> Dict[str, Any]:
        """Promote a table's pending context into Defined Knowledge.

        ``inline_context`` lets the caller supply the context text directly
        (e.g. straight from the textarea) without requiring a prior
        ``save_table_context`` call.  If both a stored context and an inline
        context are present, the inline value takes precedence so the user's
        latest edit is always what gets promoted.
        """
        # Prefer the inline value so the user never has to click
        # "Save for review" before "Accept context".
        context = str(inline_context or "").strip() or self.table_context(table)
        logger.info(
            "[KNOWLEDGE] accept_table_context table=%s namespace=%s length=%d",
            table, self.path.parent.name, len(context),
        )
        if not context:
            raise ValueError("'context' is required — type a definition and click Accept, or Save first then Accept")
        entry = defined.update_table(table, {"definition": context, "source": "USER_APPROVED", "approval_status": "APPROVED"})
        # Clear any stored draft now that it has been promoted.
        self._data["table_contexts"].pop(table, None)
        self._save()
        return entry

    def approve_term(self, term_id: str, defined: Any, data: Dict[str, Any]) -> Dict[str, Any]:
        item = self._data["business_terms"].get(term_id)
        if item is None:
            raise KeyError(term_id)
        table = str(data.get("table") or item.get("table") or "").strip()
        if not table:
            raise ValueError("A target 'table' is required")
        meaning = str(data.get("meaning") or "").strip()
        if not meaning:
            raise ValueError("'meaning' is required")
        # Pending review deliberately has no physical-column association or
        # abbreviation context; the table context supplies domain meaning.
        term = defined.upsert_business_term(table, {
            "abbreviation": data.get("abbreviation") or item.get("detected_term"),
            "meaning": meaning,
            "source": "USER_APPROVED", "approval_status": "APPROVED",
        })
        del self._data["business_terms"][term_id]
        self._save()
        return term

    def reject(self, item_type: str, item_id: str) -> bool:
        if item_type == "table":
            if item_id not in self._data["dismissed_tables"]:
                self._data["dismissed_tables"].append(item_id)
                self._save()
            return True
        if item_id in self._data["business_terms"]:
            del self._data["business_terms"][item_id]
            self._save()
            return True
        return False

    def add_feedback_recommendation(self, recommendation: Dict[str, Any]) -> bool:
        """Persist a bounded pending recommendation unless equivalent history exists."""
        identity = (recommendation.get("type"), recommendation.get("table"), recommendation.get("column"), recommendation.get("phrase"))
        for item in self._data["feedback_recommendations"]:
            if tuple(item.get(key) for key in ("type", "table", "column", "phrase")) == identity and item.get("status") in {"PENDING", "REJECTED", "APPROVED"}:
                return False
        self._data["feedback_recommendations"].append({**recommendation, "status": "PENDING"})
        self._data["feedback_recommendations"] = self._data["feedback_recommendations"][-300:]
        self._save(); return True

    def feedback_recommendations(self, table: str = "") -> List[Dict[str, Any]]:
        return [dict(item) for item in self._data["feedback_recommendations"] if not table or item.get("table") == table]

    def add_query_diagnostic(self, diagnostic: Dict[str, Any]) -> None:
        self._data["query_diagnostics"].append(diagnostic)
        self._data["query_diagnostics"] = self._data["query_diagnostics"][-200:]
        self._save()

    def query_diagnostics(self, table: str = "") -> List[Dict[str, Any]]:
        return [dict(item) for item in self._data["query_diagnostics"] if not table or table in item.get("selected_tables", [])]
