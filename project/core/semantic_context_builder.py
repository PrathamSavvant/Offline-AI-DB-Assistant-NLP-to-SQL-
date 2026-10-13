"""Approved semantic source of truth for the deterministic query pipeline."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class SemanticContextBuilder:
    """Describe table meaning from cached schema knowledge without generating SQL."""

    _DISPLAY_WORDS = {"name", "title", "label", "description", "summary"}
    _STATUS_WORDS = {"status", "state", "active", "enabled"}
    _TIMESTAMP_WORDS = {"date", "time", "created", "updated", "deleted", "at"}
    _HISTORY_WORDS = {"history", "archive", "audit", "log"}

    def __init__(
        self,
        schema: Dict[str, Any],
        semantics: Optional[Dict[str, Any]] = None,
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        relationships: Optional[Dict[str, Any]] = None,
        table_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        defined_knowledge: Any = None,
    ):
        self.tables_by_name = {table["name"]: table for table in schema.get("tables", [])}
        self.semantics = (semantics or {}).get("tables", semantics or {})
        self.annotations = annotations or {}
        self.relationships = (relationships or {}).get("relationships", [])
        self.table_profiles = table_profiles or {}
        self.defined_knowledge = defined_knowledge

    @staticmethod
    def _words(value: str) -> List[str]:
        return re.findall(r"[A-Za-z0-9]+", value.replace("_", " ").lower())

    def _related(self, table_name: str) -> List[str]:
        related = []
        for relation in self.relationships:
            if relation.get("from_table") == table_name:
                related.append(relation.get("to_table"))
            elif relation.get("to_table") == table_name:
                related.append(relation.get("from_table"))
        return list(dict.fromkeys(name for name in related if name))[:2]

    def _summary(self, table_name: str, question: str) -> Dict[str, Any]:
        approved = (
            self.defined_knowledge.table(table_name)
            if self.defined_knowledge is not None and hasattr(self.defined_knowledge, "table")
            else {}
        )
        profile = self.table_profiles.get(table_name)
        if profile:
            question_words = set(self._words(question))
            business = [
                name for name in profile.get("business_columns", [])
                if question_words & set(self._words(name))
            ] or profile.get("business_columns", [])[:4]
            return {
                "table": table_name,
                "purpose": approved.get("definition") or profile.get("purpose", ""),
                "business_entity": approved.get("business_entity") or approved.get("business_name") or profile.get("business_name", table_name.replace("_", " ").title()),
                "primary_identifier": profile.get("identity_columns", [])[:2],
                "display_columns": profile.get("display_columns", [])[:3],
                "business_columns": business[:4],
                "frequently_joined_tables": profile.get("related_tables", [])[:2],
                "historical": "likely" if "history" in self._words(table_name) else "unknown",
                "status_column": next((name for name in profile.get("commonly_filtered_columns", []) if "status" in self._words(name)), None),
                "timestamp_columns": [name for name in profile.get("commonly_sorted_columns", []) if {"date", "created", "updated"} & set(self._words(name))][:2],
                "approved": approved,
            }
        table = self.tables_by_name[table_name]
        annotation = self.annotations.get(table_name, {})
        semantic = self.semantics.get(table_name, {})
        columns = table.get("columns", [])
        names = [column.get("name", "") for column in columns]
        primary = list(table.get("primary_key", [])) or [
            column["name"] for column in columns if column.get("is_primary_key")
        ]
        display = [name for name in names if self._DISPLAY_WORDS & set(self._words(name))][:3]
        question_words = set(self._words(question))
        business = [
            name for name in names
            if name not in primary and name not in display and question_words & set(self._words(name))
        ][:4]
        status = next((name for name in names if self._STATUS_WORDS & set(self._words(name))), None)
        timestamps = [name for name in names if self._TIMESTAMP_WORDS & set(self._words(name))][:2]
        is_historical = bool(self._HISTORY_WORDS & set(self._words(table_name)))
        purpose = annotation.get("meaning") or semantic.get("summary") or f"Stores {table_name.replace('_', ' ')} records."
        return {
            "table": table_name,
            "purpose": approved.get("definition") or purpose,
            "business_entity": approved.get("business_entity") or approved.get("business_name") or table_name.replace("_", " ").title(),
            "primary_identifier": primary[:2],
            "display_columns": display,
            "business_columns": business,
            "frequently_joined_tables": self._related(table_name),
            # This is deliberately conservative: a name-based signal is
            # labelled as likely, never asserted as a business fact.
            "historical": "likely" if is_historical else "unknown",
            "status_column": status,
            "timestamp_columns": timestamps,
            "approved": approved,
        }

    def build(self, context: Dict[str, Any]) -> Dict[str, Any]:
        question = context.get("question", "")
        question_words = set(self._words(question))
        entries = [
            self._summary(table_name, context.get("question", ""))
            for table_name in context.get("selected_tables", [])
            if table_name in self.tables_by_name
        ]
        evidence, relevant_columns, predicates, conflicts, approved_terms = [], {}, [], [], []
        seen_predicates = {}
        for item in entries:
            table, approved = item["table"], item.pop("approved", {})
            matches = []
            for label, values in (("business_entity", [approved.get("business_entity", ""), approved.get("business_name", "")]),
                                  ("alias", [*approved.get("aliases", []), *approved.get("synonyms", [])]),
                                  ("intended_usage", approved.get("intended_usage", []))):
                for value in values:
                    words = set(self._words(str(value)))
                    if words and words <= question_words:
                        matches.append({"type": label, "matched_text": value})
            for token, term in approved.get("abbreviations", {}).items():
                term = term if isinstance(term, dict) else {"meaning": str(term)}
                vocabulary = [token, term.get("meaning", ""), term.get("context", "")]
                if any(set(self._words(str(value))) and set(self._words(str(value))) <= question_words for value in vocabulary):
                    matches.append({"type": "abbreviation", "matched_text": token})
                    approved_terms.append({"table": table, "token": token, "meaning": term.get("meaning", ""), "context": term.get("context", "")})
            for column, definition in approved.get("columns", {}).items():
                phrases = [definition.get("business_meaning", ""), *definition.get("aliases", []), *definition.get("synonyms", []), *definition.get("common_phrases", []), *definition.get("intended_usage", [])]
                if any(set(self._words(str(phrase))) and set(self._words(str(phrase))) <= question_words for phrase in phrases):
                    matches.append({"type": "column", "column": column})
                    relevant_columns.setdefault(table, []).append(column)
                for phrase, value in definition.get("important_values", {}).items():
                    if set(self._words(str(phrase))) and set(self._words(str(phrase))) <= question_words:
                        relevant_columns.setdefault(table, []).append(column)
                        predicate = {"table": table, "column": column, "operator": "=", "value": value, "reason": "approved important value"}
                        key = (column, str(value).lower())
                        if key in seen_predicates and seen_predicates[key]["table"] != table:
                            conflicts.append({"phrase": phrase, "candidates": [seen_predicates[key], predicate]})
                        else:
                            seen_predicates[key] = predicate; predicates.append(predicate)
            for mapping in approved.get("predicate_mappings", []):
                phrase = str(mapping.get("phrase") or "")
                if phrase and set(self._words(phrase)) <= question_words:
                    predicate = {"table": table, "column": mapping.get("column"), "operator": mapping.get("operator", "="), "value": mapping.get("value"), "reason": "approved predicate mapping"}
                    key = (predicate["column"], str(predicate["value"]).lower())
                    if key in seen_predicates and seen_predicates[key]["table"] != table:
                        conflicts.append({"phrase": phrase, "candidates": [seen_predicates[key], predicate]})
                    else:
                        seen_predicates[key] = predicate; predicates.append(predicate)
            if matches:
                evidence.append({"table": table, "matches": matches})
        relevant_columns = {table: list(dict.fromkeys(columns)) for table, columns in relevant_columns.items()}
        return {"tables": entries, "evidence": evidence, "relevant_columns": relevant_columns,
                "approved_predicates": predicates, "approved_terms": approved_terms, "conflicts": conflicts}
