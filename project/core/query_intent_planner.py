"""Deterministic business-intent planning before SQL syntax generation."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ResolvedFilter:
    table: str
    column: str
    operator: str
    value: Any
    reason: str


@dataclass(frozen=True)
class QueryIntent:
    primary_entity: Optional[str]
    result_type: str
    projection: List[str]
    filters: List[ResolvedFilter] = field(default_factory=list)
    joins: List[str] = field(default_factory=list)
    sort: Optional[str] = None
    aggregation: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class QueryIntentPlanner:
    """Resolve *what* the question means using only loaded metadata.

    This planner intentionally does not produce SQL.  It is conservative: a
    filter is emitted only when one table/column target is unambiguous from the
    question, schema, relationships, or approved business dictionary.
    """

    _COUNT = (r"\bhow many\b", r"\bcount\b", r"\bnumber of\b")
    _AGGREGATE = (r"\baverage\b", r"\bavg\b", r"\btotal\b", r"\bsum\b", r"\bminimum\b", r"\bmaximum\b")
    _COMPARATORS = ((r"\b(?:above|over|greater than)\s+(\d+(?:\.\d+)?)", ">"), (r"\b(?:below|under|less than)\s+(\d+(?:\.\d+)?)", "<"))
    _OUTPUT_WORDS = {"show", "list", "find", "display", "get", "give", "which", "what"}
    _LOOKUP_LABELS = {"group", "type", "class", "category", "department", "unit", "status"}
    _CATEGORICAL_HINTS = {"status", "state", "class", "type", "category", "kind"}
    _STOP_WORDS = {
        "in", "the", "for", "a", "an", "under", "over", "with", "by", "at",
        "from", "to", "of", "on", "and", "or", "is", "are", "show", "list",
        "find", "get", "display", "which", "what", "all", "any", "each",
    }

    def __init__(
        self,
        schema: Dict[str, Any],
        semantics: Optional[Dict[str, Any]] = None,
        relationships: Optional[Dict[str, Any]] = None,
        dictionary: Any = None,
        table_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        defined_knowledge: Any = None,
    ):
        self.tables = {table["name"]: table for table in schema.get("tables", [])}
        self.semantics = (semantics or {}).get("tables", semantics or {})
        self.relationships = (relationships or {}).get("relationships", [])
        self.dictionary = dictionary
        self.table_profiles = table_profiles or {}
        self.defined_knowledge = defined_knowledge

    @staticmethod
    def _words(value: str) -> List[str]:
        return re.findall(r"[A-Za-z0-9]+", value.replace("_", " ").lower())

    def _expanded_words(self, question: str) -> set[str]:
        words = set(self._words(question))
        if self.dictionary is not None:
            try:
                for entry in self.dictionary.resolve_text(question):
                    for phrase in [entry.get("expansion", ""), *entry.get("synonyms", []), *entry.get("aliases", [])]:
                        words.update(self._words(phrase))
            except Exception:
                logger.debug("Dictionary expansion unavailable to intent planner", exc_info=True)
        words.update(
            word[:-1] if word.endswith("s") and len(word) > 3 else word + "s"
            for word in list(words)
        )
        return words

    def _result_type(self, question: str) -> str:
        lower = question.lower()
        if any(re.search(pattern, lower) for pattern in self._COUNT):
            return "COUNT"
        if any(re.search(pattern, lower) for pattern in self._AGGREGATE):
            return "AGGREGATE"
        return "ENTITY_LIST"

    def _entity_score(self, table_name: str, question: str, question_words: set[str], presentation: Dict[str, Any]) -> int:
        table_words = set(self._words(table_name))
        score = len(table_words & question_words) * 12
        semantics = self.semantics.get(table_name, {})
        profile = self.table_profiles.get(table_name, {})
        approved = self.defined_knowledge.table(table_name) if self.defined_knowledge is not None and hasattr(self.defined_knowledge, "table") else {}
        for phrase in [approved.get("business_entity", ""), approved.get("business_name", ""), *approved.get("aliases", []), *approved.get("synonyms", []), *approved.get("intended_usage", [])]:
            words = set(self._words(str(phrase)))
            if words and words <= question_words:
                score += 24 if phrase == approved.get("business_entity", "") else 16
        score += len(set(_ for _ in self._words(" ".join(profile.get("aliases", []))) if _) & question_words) * 10
        score += len(set(self._words(str(profile.get("abbreviations", {})))) & question_words) * 10
        score += len(set(profile.get("planner_hints", {}).get("business_tags", [])) & question_words) * 8
        score += len(set(semantics.get("business_tags", [])) & question_words) * 8
        for column in self.tables[table_name].get("columns", []):
            score += len(set(self._words(column["name"])) & question_words) * 2
        entity = next((item for item in presentation.get("entities", []) if item.get("table") == table_name), {})
        # A table that has descriptive/presentation columns is more likely the
        # output entity than a pure lookup/link table.
        score += min(len(entity.get("display_columns", [])), 2)
        # "in OE group" describes a lookup/filter entity, not normally the
        # object requested for output. This is grammar-based and applies to
        # any table named by the value-label phrase, not a hardcoded table.
        singular = table_name.rstrip("s").replace("_", " ")
        if re.search(r"\bin\s+[A-Za-z0-9]+\s+" + re.escape(singular) + r"\b", question.lower()):
            score -= 15
        return score

    def _primary_entity(self, context: Dict[str, Any], question_words: set[str]) -> Optional[str]:
        selected = context.get("selected_tables", [])
        if not selected:
            return None
        presentation = context.get("presentation_context", {})
        ranked = sorted(
            selected,
            key=lambda name: (-self._entity_score(name, context.get("question", ""), question_words, presentation), name),
        )
        # Prefer a table whose own columns/business tags match the requested
        # concept over a lookup table that matched only a filter word.
        return ranked[0]

    def _projection(self, primary: Optional[str], context: Dict[str, Any], question_words: set[str], result_type: str) -> List[str]:
        if primary is None or result_type != "ENTITY_LIST":
            return []
        presentation = next(
            (item for item in context.get("presentation_context", {}).get("entities", []) if item.get("table") == primary),
            {},
        )
        columns = []
        for name in [*presentation.get("identity_columns", []), *presentation.get("display_columns", [])]:
            columns.append(f"{primary}.{name}")
        for column in self.tables[primary].get("columns", []):
            name = column["name"]
            if name.endswith("_id") and name not in presentation.get("identity_columns", []):
                continue
            if set(self._words(name)) & question_words:
                columns.append(f"{primary}.{name}")
        return list(dict.fromkeys(columns))

    def _candidate_columns(self, table_name: str, words: set[str]) -> List[str]:
        candidates = []
        for column in self.tables[table_name].get("columns", []):
            column_words = set(self._words(column["name"]))
            if column_words & words:
                candidates.append(column["name"])
        return candidates

    def _numeric_filters(self, context: Dict[str, Any], question: str, question_words: set[str]) -> List[ResolvedFilter]:
        filters = []
        for pattern, operator in self._COMPARATORS:
            match = re.search(pattern, question.lower())
            if not match:
                continue
            value = float(match.group(1)) if "." in match.group(1) else int(match.group(1))
            candidates = []
            for table_name in context.get("selected_tables", []):
                for column in self._candidate_columns(table_name, question_words):
                    datatype = next((item.get("datatype", "") for item in self.tables[table_name].get("columns", []) if item["name"] == column), "")
                    if any(token in str(datatype).lower() for token in ("int", "decimal", "numeric", "float", "double")):
                        candidates.append((table_name, column))
            if len(candidates) == 1:
                table, column = candidates[0]
                filters.append(ResolvedFilter(table, column, operator, value, "numeric comparison in question"))
        return filters

    _ENTITY_NOUNS = {
        "manager", "managers", "employee", "employees", "worker", "workers",
        "record", "records", "table", "tables", "detail", "details",
        "list", "all", "each", "every", "total", "count", "show", "get", "which", "what",
        "data", "info", "information", "item", "items", "history", "unique", "distinct",
        "name", "names", "job", "jobs", "title", "titles", "average", "avg", "sum",
        "company", "system", "number", "numbers", "id", "ids", "identifier", "identifiers",
    }

    def _lookup_filters(self, context: Dict[str, Any], question: str) -> List[ResolvedFilter]:
        filters = []
        selected = context.get("selected_tables", [])
        tokens = re.findall(r"\b[A-Za-z0-9_-]+\b", question)
        candidates = []

        # Build set of selected table name components to prevent table names from becoming lookup values
        selected_components = set()
        for t in selected:
            selected_components.update(self._words(t))

        for i in range(len(tokens) - 1):
            t1, t2 = tokens[i], tokens[i + 1]
            if t2.lower() in self._LOOKUP_LABELS and t1.lower() not in self._STOP_WORDS and len(t1) >= 2:
                if t1.lower() not in self._ENTITY_NOUNS and t1.lower() not in selected_components:
                    candidates.append((t1, t2.lower()))
            elif t1.lower() in self._LOOKUP_LABELS and t2.lower() not in self._STOP_WORDS and len(t2) >= 2:
                if t2.lower() not in self._ENTITY_NOUNS and t2.lower() not in selected_components:
                    candidates.append((t2, t1.lower()))

        for value, label in candidates:
            table_matches = [name for name in selected if label in self._expanded_words(name)]
            if len(table_matches) != 1:
                continue
            table = table_matches[0]
            name_columns = [column["name"] for column in self.tables[table].get("columns", []) if "name" in self._words(column["name"])]
            if len(name_columns) == 1:
                filters.append(ResolvedFilter(table, name_columns[0], "=", value.upper(), f"{label} lookup value"))
        return filters

    def _categorical_filters(self, primary: Optional[str], question: str, question_words: set[str]) -> List[ResolvedFilter]:
        if primary is None:
            return []
        match = re.search(r"\bunder\s+([A-Za-z][A-Za-z _-]{1,40}?)(?:\s+in\b|$)", question, re.IGNORECASE)
        if not match:
            return []
        value = match.group(1).strip()
        value_words = set(self._words(value))
        candidates = []
        for column in self.tables[primary].get("columns", []):
            column_words = set(self._words(column["name"]))
            if column_words & value_words and column_words & self._CATEGORICAL_HINTS:
                candidates.append(column["name"])
        if len(candidates) == 1:
            return [ResolvedFilter(primary, candidates[0], "=", value.title(), "categorical business concept")]
        return []

    @staticmethod
    def _joins(context: Dict[str, Any]) -> List[str]:
        return [f"{edge['from']}.{edge['from_column']} = {edge['to']}.{edge['to_column']}" for edge in context.get("join_plan", {}).get("join_edges", [])]

    def build(self, context: Dict[str, Any]) -> QueryIntent:
        question = context.get("question", "")
        question_words = self._expanded_words(question)
        result_type = self._result_type(question)
        primary = self._primary_entity(context, question_words)
        has_canonical_predicates = "predicate_manifest" in context
        # PredicatePlanner is the sole predicate authority in the production
        # pipeline. Standalone QueryIntentPlanner usage retains its legacy
        # heuristics for backwards-compatible diagnostics and unit use.
        filters = [] if has_canonical_predicates else self._numeric_filters(context, question, question_words)
        for item in context.get("predicate_manifest", {}).get("filters", []):
            if item.get("table") in context.get("selected_tables", []) and item.get("column"):
                filters.append(ResolvedFilter(item["table"], item["column"], item.get("operator", "="), item.get("value"), item.get("reason", "resolved predicate")))
        if not has_canonical_predicates:
            filters.extend(self._lookup_filters(context, question))
            filters.extend(self._categorical_filters(primary, question, question_words))
        unique_filters = list(dict.fromkeys(filters))
        intent = QueryIntent(
            primary_entity=primary,
            result_type=result_type,
            projection=self._projection(primary, context, question_words, result_type),
            filters=unique_filters,
            joins=self._joins(context),
            aggregation="COUNT" if result_type == "COUNT" else None,
        )
        logger.info("Query intent primary entity: %s", intent.primary_entity)
        logger.info("Query intent projection: %s", intent.projection)
        logger.info("Query intent filters: %s", [asdict(item) for item in intent.filters])
        logger.info("Query intent joins: %s", intent.joins)
        return intent
