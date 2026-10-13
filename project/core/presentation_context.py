"""Deterministic guidance for presenting records in generated SQL results.

This module deliberately describes schema semantics only.  It never generates,
parses, or changes SQL; the LLM remains responsible for query construction and
the existing validator remains responsible for safety.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_IDENTITY_TERMS = ("id", "identifier", "number", "no", "code", "key")
_DISPLAY_TERMS = ("name", "title", "label", "description", "summary")
_BUSINESS_TAGS = {"financial", "temporal", "status", "quantity", "contact", "geographic"}


class PresentationContextBuilder:
    """Infer concise, schema-derived display guidance for selected tables.

    The builder consumes existing cached knowledge and user-owned metadata.  Its
    output is intentionally provider- and prompt-format-independent so it can be
    reused by any future presentation or reporting surface.
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        semantics: Optional[Dict[str, Any]] = None,
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        dictionary: Any = None,
        relationships: Optional[Dict[str, Any]] = None,
        table_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.tables_by_name = {table["name"]: table for table in schema.get("tables", [])}
        self.semantics = (semantics or {}).get("tables", semantics or {})
        self.annotations = annotations if annotations is not None else {}
        self.dictionary = dictionary
        self.relationships = (relationships or {}).get("relationships", [])
        self.table_profiles = table_profiles or {}

    @staticmethod
    def _words(value: str) -> List[str]:
        return [word.lower() for word in re.findall(r"[A-Za-z0-9]+", value.replace("_", " "))]

    def _human_name(self, table_name: str) -> str:
        annotation = self.annotations.get(table_name, {})
        if annotation.get("meaning"):
            return annotation["meaning"]
        if self.dictionary is not None:
            expanded = self.dictionary.expand_name(table_name).get("expanded", "")
            if expanded and expanded.lower() != table_name.lower():
                return expanded
        return " ".join(self._words(table_name)).title()

    def _primary_keys(self, table: Dict[str, Any]) -> List[str]:
        keys = list(table.get("primary_key", []))
        keys.extend(column["name"] for column in table.get("columns", []) if column.get("is_primary_key"))
        return list(dict.fromkeys(keys))

    def _identity_columns(self, table: Dict[str, Any], column_tags: Dict[str, List[str]]) -> List[str]:
        identities = self._primary_keys(table)
        for column in table.get("columns", []):
            name = column["name"]
            words = self._words(name)
            tags = set(column_tags.get(name, []))
            if "identifier" in tags or any(term in words for term in _IDENTITY_TERMS):
                identities.append(name)
        return list(dict.fromkeys(identities))[:3]

    def _display_columns(
        self, table: Dict[str, Any], column_tags: Dict[str, List[str]], identities: List[str]
    ) -> List[str]:
        ranked = []
        for position, column in enumerate(table.get("columns", [])):
            name = column["name"]
            if name in identities:
                continue
            words = self._words(name)
            tags = set(column_tags.get(name, []))
            score = 0
            if "descriptive" in tags:
                score += 20
            score += sum(10 for term in _DISPLAY_TERMS if term in words)
            if score:
                ranked.append((score, -position, name))

        # A table without descriptive naming still benefits from a readable
        # text column. This is a generic type-based fallback, not a table rule.
        if not ranked:
            for position, column in enumerate(table.get("columns", [])):
                name = column["name"]
                if name in identities:
                    continue
                datatype = (column.get("datatype") or column.get("full_type") or "").lower()
                if any(token in datatype for token in ("char", "text", "enum")):
                    ranked.append((1, -position, name))
        ranked.sort(reverse=True)
        return [name for _, _, name in ranked[:3]]

    def _business_columns(
        self, table: Dict[str, Any], column_tags: Dict[str, List[str]], question: str,
        identities: List[str], display_columns: List[str],
    ) -> List[str]:
        question_words = set(self._words(question))
        ranked = []
        for position, column in enumerate(table.get("columns", [])):
            name = column["name"]
            if name in identities or name in display_columns:
                continue
            tags = set(column_tags.get(name, []))
            words = set(self._words(name))
            score = len(question_words & words) * 10 + len(tags & _BUSINESS_TAGS)
            if score:
                ranked.append((score, -position, name))
        ranked.sort(reverse=True)
        return [name for _, _, name in ranked[:4]]

    def _relationship_summary(self, table_name: str) -> List[str]:
        summaries = []
        for relationship in self.relationships:
            if relationship.get("from_table") == table_name:
                summaries.append(
                    f"{relationship.get('from_column')} → "
                    f"{relationship.get('to_table')}.{relationship.get('to_column')}"
                )
            elif relationship.get("to_table") == table_name:
                summaries.append(
                    f"{relationship.get('from_table')}.{relationship.get('from_column')} → "
                    f"{relationship.get('to_column')}"
                )
        return summaries[:2]

    def build(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Return presentation metadata for the context's already-selected tables."""
        entities = []
        selected_tables = context.get("selected_tables", [])
        primary_table = context.get("join_plan", {}).get("base_table") or (selected_tables[0] if selected_tables else None)
        for table_name in selected_tables:
            table = self.tables_by_name.get(table_name)
            if table is None:
                continue
            table_semantics = self.semantics.get(table_name, {})
            column_tags = table_semantics.get("column_semantics", {})
            profile = self.table_profiles.get(table_name, {})
            identities = profile.get("identity_columns") or self._identity_columns(table, column_tags)
            display_columns = profile.get("display_columns") or self._display_columns(table, column_tags, identities)
            entities.append({
                "table": table_name,
                "human_name": profile.get("business_name") or self._human_name(table_name),
                "primary_entity": table_name == primary_table,
                "primary_key": self._primary_keys(table),
                "identity_columns": identities,
                "display_columns": display_columns,
                "natural_display_name": ", ".join(display_columns),
                "important_business_columns": profile.get("business_columns") or self._business_columns(
                    table, column_tags, context.get("question", ""), identities, display_columns,
                ),
                "relationships": [
                    f"{item.get('column')} → {item.get('related_table')}.{item.get('related_column')}"
                    for item in profile.get("relationships", [])[:2]
                ] or self._relationship_summary(table_name),
            })
        return {"entities": entities}
