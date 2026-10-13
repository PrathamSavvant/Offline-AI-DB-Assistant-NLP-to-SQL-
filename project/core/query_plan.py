"""Deterministic planning of *what* an NL-to-SQL result must return.

``QueryPlanBuilder`` deliberately stops before SQL construction: it selects
entities and source columns from cached metadata, while the LLM still decides
FROM/JOIN/WHERE/GROUP BY/ORDER BY and SQL syntax.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class QueryPlan:
    """Single provider-neutral plan handed from deterministic reasoning to Llama.

    The original projection fields remain for backward compatibility.  The
    additional fields make table, knowledge, predicate and FK decisions
    inspectable before SQL composition begins.
    """

    primary_entity: str | None = None
    result_type: str = "ENTITY"
    primary_key: List[str] = field(default_factory=list)
    record_grain: str = ""
    table_roles: Dict[str, str] = field(default_factory=dict)
    identity_columns: List[str] = field(default_factory=list)
    display_columns: List[str] = field(default_factory=list)
    requested_attributes: List[str] = field(default_factory=list)
    required_projection: List[str] = field(default_factory=list)
    relationship_summary: List[str] = field(default_factory=list)
    database_identity: str = ""
    original_question: str = ""
    normalized_question: str = ""
    status: str = "ready"
    selected_tables: List[str] = field(default_factory=list)
    table_selection_reasons: Dict[str, List[str]] = field(default_factory=dict)
    knowledge_matches: List[Dict[str, Any]] = field(default_factory=list)
    resolved_predicates: List[Dict[str, Any]] = field(default_factory=list)
    unresolved_terms: List[str] = field(default_factory=list)
    operation: Dict[str, Any] = field(default_factory=dict)
    requested_tables: List[str] = field(default_factory=list)
    bridge_tables: List[str] = field(default_factory=list)
    verified_join_path: List[Dict[str, Any]] = field(default_factory=list)
    approved_join_path: List[Dict[str, Any]] = field(default_factory=list)
    ambiguities: List[Dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class QueryPlanBuilder:
    """Build a deterministic result plan from already-selected cached context."""

    _COUNT_PATTERNS = (r"\bhow many\b", r"\bcount\b", r"\bnumber of\b")
    _BOOLEAN_PATTERNS = (r"\bdoes .+ exist\b", r"\bis there\b", r"\bare there any\b", r"\bany .+\?")
    _AGGREGATE_PATTERNS = (r"\baverage\b", r"\bavg\b", r"\btotal\b", r"\bsum\b", r"\bminimum\b", r"\bmaximum\b")
    _ENTITY_SUPERLATIVES = (r"\bhighest\b", r"\blowest\b", r"\btop\b", r"\bbottom\b")
    _CURRENT_PATTERNS = (r"\bcurrent(?:ly)?\b", r"\bnow\b", r"\bat present\b", r"\bstill\b", r"\bwhere (?:is|are)\b")

    @classmethod
    def _result_type(cls, question: str) -> str:
        question_lower = question.lower()
        if any(re.search(pattern, question_lower) for pattern in cls._COUNT_PATTERNS):
            return "COUNT"
        if any(re.search(pattern, question_lower) for pattern in cls._BOOLEAN_PATTERNS):
            return "BOOLEAN"
        # "highest salary" normally asks for an entity row, not MAX(salary).
        if any(re.search(pattern, question_lower) for pattern in cls._ENTITY_SUPERLATIVES):
            return "ENTITY"
        if any(re.search(pattern, question_lower) for pattern in cls._AGGREGATE_PATTERNS):
            return "AGGREGATE"
        return "ENTITY"

    @staticmethod
    def _question_words(question: str) -> set[str]:
        words = re.findall(r"[A-Za-z0-9]+", question.lower().replace("_", " "))
        # Basic singular matching keeps the decision deterministic while letting
        # "salaries" map to a schema column named ``salary``.
        singulars = {
            (word[:-3] + "y") if word.endswith("ies") and len(word) > 4 else word[:-1]
            for word in words if word.endswith("s") and len(word) > 3
        }
        return set(words) | singulars

    @classmethod
    def _requested_attributes(cls, context: Dict[str, Any], presentation: Dict[str, Any]) -> List[str]:
        words = cls._question_words(context.get("question", ""))
        requested = []
        display_by_table = {entry["table"]: set(entry.get("display_columns", [])) for entry in presentation.get("entities", [])}
        identity_by_table = {entry["table"]: set(entry.get("identity_columns", [])) for entry in presentation.get("entities", [])}
        for table_name in context.get("selected_tables", []):
            table = context.get("tables_by_name", {}).get(table_name)
            # QueryContextBuilder does not expose tables; use plan metadata when
            # present and otherwise rely on the compact selected-table snapshots.
            if table is None:
                table = next((item for item in context.get("selected_table_metadata", []) if item.get("name") == table_name), {})
            for column in table.get("columns", []):
                column_name = column.get("name", "")
                column_words = set(re.findall(r"[A-Za-z0-9]+", column_name.lower().replace("_", " ")))
                if not (words & column_words):
                    continue
                if column_name in display_by_table.get(table_name, set()) or column_name in identity_by_table.get(table_name, set()):
                    continue
                requested.append(f"{table_name}.{column_name}")
        return list(dict.fromkeys(requested))

    def __init__(self, schema: Dict[str, Any]):
        self.schema = schema
        self.tables_by_name = {table["name"]: table for table in schema.get("tables", [])}

    @staticmethod
    def normalize(question: str) -> str:
        """Normalize spacing/case for matching without discarding original terms."""
        return " ".join(str(question or "").strip().lower().split())

    @staticmethod
    def _selection_reasons(context: Dict[str, Any]) -> Dict[str, List[str]]:
        reasons: Dict[str, List[str]] = {}
        scored = {
            item.get("table"): item for item in context.get("scored_tables", [])
            if item.get("table")
        }
        bridges = set(context.get("bridge_tables", []))
        business_intents = {
            item.get("table"): item
            for item in context.get("business_intent", {}).get("table_intents", [])
            if item.get("table")
        }
        for table in context.get("selected_tables", []):
            record = scored.get(table, {})
            matches = list(record.get("matches", []))
            labels = list(record.get("grounding_reasons", []))
            for match in matches:
                prefix = str(match).split(":", 1)[0].split(" ", 1)[0]
                label = {
                    "table_name": "exact_schema_match",
                    "column": "exact_column_match",
                    "defined_abbr": "approved_abbreviation",
                    "manual_definition": "approved_table_context",
                    "meaning_overlap": "approved_table_context",
                    "alias": "approved_alias_match",
                    "defined_synonym": "approved_synonym_match",
                    "approved_alias": "approved_alias_match",
                    "approved_entity": "approved_entity_match",
                    "approved_phrase": "approved_phrase_match",
                    "approved_column": "approved_column_match",
                    "approved_predicate": "approved_predicate_match",
                    "dictionary_match": "approved_dictionary_match",
                    "domain": "schema_domain_match",
                    "tag": "schema_business_concept_match",
                    "query_coverage": "deterministic_schema_term_match",
                }.get(prefix)
                if label:
                    labels.append(label)
            if table in bridges:
                labels.append("required_fk_bridge")
            business = business_intents.get(table, {})
            if table == context.get("business_intent", {}).get("primary_table"):
                labels.append("primary_business_entity")
            if business.get("role") in {"support", "entity+support"}:
                labels.append("explicit_attribute_or_predicate_support")
            reasons[table] = list(dict.fromkeys(labels))
        return reasons

    def build(self, context: Dict[str, Any]) -> QueryPlan:
        presentation = context.get("presentation_context", {})
        entities = presentation.get("entities", [])
        resolved_primary = context.get("business_intent", {}).get("primary_table")
        primary = next(
            (entry for entry in entities if entry.get("table") == resolved_primary),
            next((entry for entry in entities if entry.get("primary_entity")), entities[0] if entities else {}),
        )
        primary_table = primary.get("table")
        result_type = self._result_type(context.get("question", ""))

        # Make the selected table metadata available only while planning. It is
        # removed before the context reaches any API or prompt boundary.
        planning_context = dict(context)
        planning_context["tables_by_name"] = self.tables_by_name
        requested = self._requested_attributes(planning_context, presentation)

        projection = []
        if result_type == "ENTITY" and primary_table:
            projection.extend(f"{primary_table}.{column}" for column in primary.get("identity_columns", []))
            projection.extend(f"{primary_table}.{column}" for column in primary.get("display_columns", []))
            projection.extend(requested)
        projection = list(dict.fromkeys(projection))

        relationships = []
        for entity in entities:
            relationships.extend(entity.get("relationships", []))
        join_plan = context.get("join_plan", {})
        predicate_manifest = context.get("predicate_manifest", {})
        repository = context.get("_defined_knowledge")
        defined = repository.all() if repository is not None and hasattr(repository, "all") else {}
        intent = context.get("query_intent", {})
        selected = list(context.get("selected_tables", []))
        current_requested = any(re.search(pattern, context.get("question", "").lower()) for pattern in self._CURRENT_PATTERNS)
        table_roles = {table: str(defined.get(table, {}).get("record_role") or "") for table in selected}
        resolved_predicates = list(predicate_manifest.get("filters", []))
        current_rule_conflicts = []
        invalid_current_rules = []
        unruled_history = []
        if current_requested:
            for table in selected:
                knowledge = defined.get(table, {})
                rule = knowledge.get("current_record") or {}
                column = str(rule.get("column") or "")
                schema_columns = {item.get("name") for item in self.tables_by_name.get(table, {}).get("columns", [])}
                if column and column in schema_columns and rule.get("value") is not None:
                    conflicting = [item for item in resolved_predicates if item.get("table") == table and item.get("column") == column and item.get("value") != rule["value"]]
                    if conflicting:
                        current_rule_conflicts.append({"reason": f"Approved current-record rule conflicts with another filter on {table}.{column}."})
                    elif not any(item.get("table") == table and item.get("column") == column and item.get("value") == rule["value"] for item in resolved_predicates):
                        resolved_predicates.append({"table": table, "column": column, "operator": "=", "value": rule["value"], "source": "approved_current_record_rule"})
                elif rule:
                    invalid_current_rules.append(table)
                elif table_roles[table] == "event_history":
                    unruled_history.append(table)
        selection_reasons = self._selection_reasons(context)
        ambiguities = [
            *predicate_manifest.get("ambiguities", []),
            *join_plan.get("ambiguous_paths", []),
            *current_rule_conflicts,
        ]
        failure_reason = None
        status = "ready"
        predicate_tables = {
            item.get("table") for item in predicate_manifest.get("filters", []) if item.get("table")
        }
        requested_tables = list(join_plan.get("requested_tables", selected))
        ungrounded_tables = [
            table for table in requested_tables
            if table not in set(context.get("bridge_tables", []))
            and not selection_reasons.get(table)
            and table not in predicate_tables
        ]
        if not selected:
            status = "failed"
            failure_reason = context.get("error") or "No database tables could be resolved for the question."
        elif ambiguities:
            status = "ambiguous"
        elif invalid_current_rules:
            status = "failed"
            failure_reason = "Approved current-record rule no longer matches the schema for table(s): " + ", ".join(invalid_current_rules) + "."
        elif unruled_history:
            status = "failed"
            failure_reason = "Current records cannot be determined for history table(s) without an approved current-record rule: " + ", ".join(unruled_history) + "."
        elif join_plan.get("unreachable_tables"):
            status = "failed"
            failure_reason = (
                "No verified foreign-key path or approved business relationship path exists between the required tables: "
                + ", ".join(join_plan["unreachable_tables"])
                + "."
            )
        elif join_plan.get("join_edges") and not join_plan.get(
            "all_joins_approved", join_plan.get("all_joins_verified", False)
        ):
            status = "failed"
            failure_reason = "The proposed join path is not fully backed by verified MySQL foreign keys or approved business relationships."
        elif ungrounded_tables:
            status = "failed"
            failure_reason = (
                "No sufficiently grounded database entity was found for: "
                + ", ".join(ungrounded_tables)
                + "."
            )

        knowledge_matches = list(context.get("knowledge_matches", []))
        impact = context.get("knowledge_impact", {})
        if impact.get("knowledge_entries_used"):
            knowledge_matches = list(impact["knowledge_entries_used"])

        return QueryPlan(
            primary_entity=primary_table,
            result_type=result_type,
            primary_key=list(primary.get("primary_key", [])),
            record_grain=str(defined.get(primary_table, {}).get("record_grain") or ""),
            table_roles=table_roles,
            identity_columns=list(primary.get("identity_columns", [])),
            display_columns=list(primary.get("display_columns", [])),
            requested_attributes=requested,
            required_projection=projection,
            relationship_summary=list(dict.fromkeys(relationships)),
            database_identity=str(context.get("database_identity") or self.schema.get("database") or ""),
            original_question=context.get("question", ""),
            normalized_question=self.normalize(context.get("question", "")),
            status=status,
            selected_tables=selected,
            table_selection_reasons=selection_reasons,
            knowledge_matches=knowledge_matches,
            resolved_predicates=resolved_predicates,
            unresolved_terms=list(dict.fromkeys(
                str(item.get("value") or item.get("term") or item.get("reason") or "")
                for item in ambiguities
                if item.get("value") or item.get("term") or item.get("reason")
            )),
            operation={
                "type": intent.get("result_type", result_type),
                "details": {
                    "projection": intent.get("projection", projection),
                    "aggregation": intent.get("aggregation"),
                    "sort": intent.get("sort"),
                },
            },
            requested_tables=requested_tables,
            bridge_tables=list(context.get("bridge_tables", [])),
            verified_join_path=[edge for edge in join_plan.get("join_edges", []) if edge.get("verified")],
            approved_join_path=list(join_plan.get("join_edges", [])),
            ambiguities=ambiguities,
            failure_reason=failure_reason,
        )
