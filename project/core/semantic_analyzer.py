"""
core/semantic_analyzer.py

Phase 7 — Business Meaning Detection

Infers what each table and column actually *means* in business terms,
rather than just its structural role:

    work_orders
      domain: Maintenance
      business_tags: [maintenance, repair, breakdown, service, equipment, ...]
      columns:
        work_order_id  -> identifier
        created_at     -> temporal
        status         -> status
        cost           -> financial

Table-level business tags reuse the domain keyword list from Phase 3
(config/domain_rules.py) — a table's domain IS its semantic field, so we
surface the whole keyword set for that domain rather than just the single
keyword that happened to match its name. Column-level tags come from
config/column_semantic_patterns.py.

This module takes the already-scanned schema (Phase 1) and grouping data
(Phase 3) as input — it does NOT touch the database.
"""

from typing import Any, Dict, List, Optional

from config.column_semantic_patterns import COLUMN_SEMANTIC_PATTERNS
from config.domain_rules import DOMAIN_RULES, UNCATEGORIZED_DOMAIN
from core.business_dictionary import BusinessDictionaryStore
from core.logger import get_logger

logger = get_logger(__name__)


class SemanticAnalyzer:
    """
    Infers business semantics for tables and columns using domain
    classification (Phase 3) and column naming patterns.

    Usage:
        analyzer = SemanticAnalyzer(schema, grouping_data)
        result = analyzer.build_all()

    Extension: also accepts a Business Dictionary (core/business_dictionary.py)
    so that when metadata contains an abbreviation like "UI_ID", the
    analyzer internally understands "User ID" while the original column
    name is preserved untouched everywhere else in the pipeline. This is
    surfaced as an additional "dictionary_expansion" field per column —
    every previously-existing field/shape is unchanged.
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        grouping_data: Dict[str, Any],
        dictionary: Optional[BusinessDictionaryStore] = None,
    ):
        self.tables = schema.get("tables", [])
        self.table_to_domain: Dict[str, str] = grouping_data.get("table_to_domain", {})
        # Optional: callers that don't care about dictionary enrichment
        # (or predate this extension) can omit it entirely and get
        # exactly the old behavior back.
        self.dictionary = dictionary

    # ------------------------------------------------------------------
    # COLUMN-LEVEL SEMANTICS
    # ------------------------------------------------------------------

    @staticmethod
    def analyze_column(column_name: str) -> List[str]:
        """
        Return every semantic tag whose pattern matches the given column
        name. A column can match multiple tags.
        """

        name_lower = column_name.lower()
        tags = []

        for tag, patterns in COLUMN_SEMANTIC_PATTERNS.items():
            if any(pattern in name_lower for pattern in patterns):
                tags.append(tag)

        return tags

    # ------------------------------------------------------------------
    # TABLE-LEVEL SEMANTICS
    # ------------------------------------------------------------------

    def get_table_semantics(self, table: Dict[str, Any]) -> Dict[str, Any]:
        """
        Build the full semantic profile for a single table: its business
        domain, the business-concept tags that domain implies, and a
        per-column semantic breakdown.
        """

        table_name = table["name"]
        domain = self.table_to_domain.get(table_name, UNCATEGORIZED_DOMAIN)

        business_tags = DOMAIN_RULES.get(domain, [])

        column_semantics: Dict[str, List[str]] = {}
        tag_counts: Dict[str, int] = {}

        for column in table.get("columns", []):
            col_name = column["name"]
            tags = self.analyze_column(col_name)
            column_semantics[col_name] = tags

            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        summary = self._build_summary(table_name, domain, business_tags, tag_counts)

        result = {
            "table": table_name,
            "domain": domain,
            "business_tags": business_tags,
            "column_semantics": column_semantics,
            "column_tag_counts": tag_counts,
            "summary": summary,
        }

        if self.dictionary is not None:
            result["dictionary_expansion"] = self.dictionary.expand_name(table_name)
            result["column_dictionary_expansions"] = {
                column["name"]: self.dictionary.expand_name(column["name"])
                for column in table.get("columns", [])
            }

        return result

    @staticmethod
    def _build_summary(
        table_name: str,
        domain: str,
        business_tags: List[str],
        tag_counts: Dict[str, int],
    ) -> str:
        """
        Produce a short, human-readable sentence describing the table's
        business meaning — useful both for quick human review and as a
        compact string the Phase 9 Context Generator can hand to the LLM
        instead of raw column lists.
        """

        parts = [f"'{table_name}' belongs to the {domain} domain"]

        if business_tags:
            shown = ", ".join(business_tags[:5])
            parts.append(f"(related concepts: {shown})")

        if tag_counts:
            notable = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
            notable_str = ", ".join(f"{count} {tag}" for tag, count in notable[:4])
            parts.append(f"and contains {notable_str} column(s)")

        return " ".join(parts) + "."

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, Any]:
        """
        Run Phase 7 for every table and return:

            {
                "tables": {
                    table_name: { domain, business_tags, column_semantics,
                                  column_tag_counts, summary },
                    ...
                }
            }
        """

        logger.info("Starting Phase 7 business meaning detection")

        results = {}
        for table in self.tables:
            table_semantics = self.get_table_semantics(table)
            results[table_semantics["table"]] = table_semantics
            logger.info("  %s -> %s", table_semantics["table"], table_semantics["domain"])

        logger.info("Phase 7 complete: %d table(s) analyzed", len(results))

        return {"tables": results}