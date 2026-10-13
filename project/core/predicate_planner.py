"""core/predicate_planner.py

Predicate Planner — Deterministic pre-SQL resolution of WHERE clause predicates and business filters.

Combines Value Resolver outputs, numeric comparisons, dictionary terms, and schema metadata.
Resolves table column ownership, calculates confidence scores, and detects remaining ambiguities.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.value_resolver import ResolvedValue, ValueResolver

logger = get_logger(__name__)

COMPARATORS = (
    (r"\b(?:above|over|greater than|more than)\s+(\d+(?:\.\d+)?)", ">"),
    (r"\b(?:below|under|less than|fewer than)\s+(\d+(?:\.\d+)?)", "<"),
    (r"\b(?:equals?|is|at)\s+(\d+(?:\.\d+)?)", "="),
)


@dataclass(frozen=True)
class PlannedFilter:
    table: str
    column: str
    operator: str
    value: Any
    confidence: float
    reason: str
    source: str = "Predicate Planner"
    semantic_type: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PredicateManifest:
    status: str  # "RESOLVED" or "AMBIGUOUS"
    filters: List[PlannedFilter]
    ambiguities: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "filters": [f.to_dict() for f in self.filters],
            "ambiguities": self.ambiguities,
        }


class PredicatePlanner:
    """Deterministic Predicate Planner."""

    def __init__(
        self,
        schema: Dict[str, Any],
        dictionary: Any = None,
        semantics: Optional[Dict[str, Any]] = None,
        value_resolver: Optional[ValueResolver] = None,
    ):
        self.schema = schema
        self.tables = {table["name"]: table for table in schema.get("tables", [])}
        self.dictionary = dictionary
        self.semantics = semantics or {}
        self.value_resolver = value_resolver or ValueResolver(schema, dictionary, semantics)

    def _numeric_filters(self, context: Dict[str, Any]) -> List[PlannedFilter]:
        question = context.get("question", "")
        selected = context.get("selected_tables", [])
        filters: List[PlannedFilter] = []

        question_words = set(re.findall(r"[a-z0-9]+", question.lower()))

        for pattern, operator in COMPARATORS:
            match = re.search(pattern, question.lower())
            if not match:
                continue
            val_str = match.group(1)
            value = float(val_str) if "." in val_str else int(val_str)

            candidates = []
            for table_name in selected:
                table_obj = self.tables.get(table_name, {})
                for col in table_obj.get("columns", []):
                    col_name = col["name"]
                    col_words = set(re.findall(r"[a-z0-9]+", col_name.lower()))
                    datatype = str(col.get("datatype", "")).lower()
                    if (col_words & question_words) and any(
                        t in datatype for t in ("int", "decimal", "numeric", "float", "double")
                    ):
                        candidates.append((table_name, col_name))

            if len(candidates) == 1:
                t_name, c_name = candidates[0]
                filters.append(PlannedFilter(
                    table=t_name,
                    column=c_name,
                    operator=operator,
                    value=value,
                    confidence=1.0,
                    reason="numeric comparison in question",
                    source="Predicate Planner",
                ))

        return filters

    def _missing_relationship_filters(self, context: Dict[str, Any]) -> List[PlannedFilter]:
        """Resolve explicit absence of a nullable FK as `IS NULL`.

        The relation name is derived from verified schema metadata, so this
        works for arbitrary ERP roles such as manager_id, parent_no, or
        supervisor_key without a business-table allowlist.
        """
        question = str(context.get("question") or "").lower()
        filters: List[PlannedFilter] = []
        for table_name in context.get("selected_tables", []):
            table = self.tables.get(table_name, {})
            columns = {column.get("name"): column for column in table.get("columns", [])}
            for foreign_key in table.get("foreign_keys", []):
                if not foreign_key.get("verified", True):
                    continue
                column_name = str(foreign_key.get("column") or "")
                column = columns.get(column_name, {})
                if column.get("nullable") is False:
                    continue
                relation = re.sub(r"(?:_id|_no|_key)$", "", column_name.lower()).replace("_", " ")
                if not relation:
                    continue
                pattern = (
                    rf"\b(?:without(?:\s+(?:a|an|any))?|no|"
                    rf"not\s+have(?:\s+(?:a|an|any))?|"
                    rf"do(?:es)?\s+not\s+have(?:\s+(?:a|an|any))?)\s+"
                    rf"{re.escape(relation)}s?\b"
                )
                if re.search(pattern, question):
                    filters.append(PlannedFilter(
                        table=table_name,
                        column=column_name,
                        operator="IS",
                        value=None,
                        confidence=1.0,
                        reason="explicit missing verified relationship",
                    ))
        return filters

    def _identity_filters(self, context: Dict[str, Any]) -> List[PlannedFilter]:
        """Resolve an entity label followed by a numeric primary-key value."""
        question = str(context.get("question") or "").lower()
        repository = context.get("_defined_knowledge")
        filters: List[PlannedFilter] = []
        for table_name in context.get("selected_tables", []):
            table = self.tables.get(table_name, {})
            pk_columns = [
                column for column in table.get("columns", [])
                if column.get("is_primary_key")
            ]
            if len(pk_columns) != 1:
                continue
            datatype = str(pk_columns[0].get("datatype") or "").lower()
            if not any(token in datatype for token in ("int", "numeric", "decimal")):
                continue
            readable = table_name.lower().replace("_", " ")
            labels = {readable, readable.rstrip("s")}
            if repository is not None and hasattr(repository, "table"):
                entry = repository.table(table_name)
                labels.update(str(item).lower() for item in [
                    *entry.get("aliases", []), *entry.get("synonyms", []),
                    entry.get("business_name", ""), entry.get("business_entity", ""),
                ] if item)
            matches = []
            for label in sorted(labels, key=len, reverse=True):
                match = re.search(rf"\b{re.escape(label)}\s+(?:id\s+)?(\d+)\b", question)
                if match:
                    matches.append(match.group(1))
            if len(set(matches)) == 1:
                filters.append(PlannedFilter(
                    table=table_name, column=pk_columns[0]["name"], operator="=",
                    value=int(matches[0]), confidence=1.0,
                    reason="explicit entity identifier",
                ))
        return filters

    def plan(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Produce structured predicate manifest from context."""
        t0 = time.perf_counter()

        question = context.get("question", "")
        selected = context.get("selected_tables", [])
        if not selected or not question:
            return PredicateManifest("RESOLVED", [], []).to_dict()

        # 1. Resolve business values via Value Resolver
        raw_resolved = context.get("resolved_values")
        if raw_resolved is None:
            raw_resolved = self.value_resolver.resolve(context)

        resolved_values: List[ResolvedValue] = []
        for item in raw_resolved:
            if isinstance(item, ResolvedValue):
                resolved_values.append(item)
            elif isinstance(item, dict):
                resolved_values.append(ResolvedValue(
                    value=item.get("value"),
                    table=item.get("table", ""),
                    column=item.get("column", ""),
                    confidence=item.get("confidence", 1.0),
                    reason=item.get("reason", "resolved value"),
                    source=item.get("source", "Value Resolver"),
                    semantic_type=item.get("semantic_type", "UNKNOWN"),
                ))

        planned_filters: List[PlannedFilter] = []
        ambiguities: List[Dict[str, Any]] = []

        # Convert resolved values to planned filters
        for rv in resolved_values:
            planned_filters.append(PlannedFilter(
                table=rv.table,
                column=rv.column,
                operator="=",
                value=rv.value,
                confidence=rv.confidence,
                reason=rv.reason,
                source=rv.source,
                semantic_type=rv.semantic_type,
            ))

        approved_groups: Dict[str, List[PlannedFilter]] = {}
        for item in planned_filters:
            if item.source == "Knowledge Layer":
                approved_groups.setdefault(item.reason, []).append(item)
        for reason, candidates in approved_groups.items():
            targets = {(item.table, item.column, str(item.value)) for item in candidates}
            if len(targets) > 1:
                ambiguities.append({"value": "approved knowledge", "candidates": [item.to_dict() for item in candidates], "reason": "conflicting_approved_predicate_mappings"})

        # 2. Add numeric comparison filters
        num_filters = self._numeric_filters(context)
        for nf in num_filters:
            # Avoid duplicating column/operator/value
            if not any(f.table == nf.table and f.column == nf.column for f in planned_filters):
                planned_filters.append(nf)

        for missing_filter in self._missing_relationship_filters(context):
            if not any(
                item.table == missing_filter.table and item.column == missing_filter.column
                for item in planned_filters
            ):
                planned_filters.append(missing_filter)

        for identity_filter in self._identity_filters(context):
            if not any(
                item.table == identity_filter.table and item.column == identity_filter.column
                for item in planned_filters
            ):
                planned_filters.append(identity_filter)

        # 3. Detect ambiguous column references across selected tables
        # E.g. if multiple tables have same column & value match without table qualification
        seen_filters: Dict[tuple, List[PlannedFilter]] = {}
        for pf in planned_filters:
            key = (pf.operator, str(pf.value).lower())
            seen_filters.setdefault(key, []).append(pf)

        final_filters: List[PlannedFilter] = []
        for key, matches in seen_filters.items():
            if any(match.source == "Knowledge Layer" for match in matches):
                final_filters.extend(matches)
                continue
            max_conf = max(m.confidence for m in matches)
            top_matches = [m for m in matches if m.confidence == max_conf]
            if len(top_matches) == 1:
                final_filters.append(top_matches[0])
            else:
                matching_tables = []
                question_lower = question.lower()
                for m in top_matches:
                    tbl_clean = m.table.lower().replace("_", " ")
                    tbl_clean_sing = tbl_clean.rstrip("s")
                    tbl_words = set(tbl_clean.split())
                    q_words = set(re.findall(r"[a-z0-9]+", question_lower))
                    other_tables_words = set(" ".join(m2.table.lower().replace("_", " ") for m2 in top_matches if m2 != m).split())
                    if tbl_clean in question_lower or tbl_clean_sing in question_lower or ((tbl_words & q_words) and not (other_tables_words & q_words)):
                        matching_tables.append(m)
                if len(matching_tables) == 1:
                    final_filters.append(matching_tables[0])
                else:
                    ambiguities.append({
                        "value": top_matches[0].value,
                        "candidates": [m.to_dict() for m in top_matches],
                        "reason": "multiple_table_candidates",
                    })

        final_filters.sort(key=lambda item: (item.table, item.column, str(item.value)))

        # An approved mapping is explicit business truth.  A schema/value
        # heuristic may still be useful for a different column, but cannot
        # compete with it for the same predicate target.
        approved_targets = {(item.table, item.column, item.operator) for item in final_filters if item.source == "Knowledge Layer"}
        final_filters = [
            item for item in final_filters
            if item.source == "Knowledge Layer" or (item.table, item.column, item.operator) not in approved_targets
        ]

        final_filters = [item for index, item in enumerate(final_filters) if item not in final_filters[:index]]
        status = "AMBIGUOUS" if ambiguities else "RESOLVED"
        manifest = PredicateManifest(status=status, filters=final_filters, ambiguities=ambiguities)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info(
            "Predicate Planner completed in %.2f ms (status=%s, filters=%d, ambiguities=%d)",
            elapsed_ms, status, len(final_filters), len(ambiguities)
        )
        return manifest.to_dict()
