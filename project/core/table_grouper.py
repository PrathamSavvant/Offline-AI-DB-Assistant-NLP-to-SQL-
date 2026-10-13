"""
core/table_grouper.py

Phase 3 — Table Grouping

Classifies every table in the scanned schema into a business domain
(Maintenance, Inventory, Finance, Users, Administration, Sales, ...) using
editable keyword rules from config/domain_rules.py.

This module takes the already-scanned schema as input — it does NOT touch
the database. Keeping it decoupled from scanning means you can re-run
grouping instantly after editing domain_rules.py, without re-scanning
the whole database.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional

from config.domain_rules import DOMAIN_RULES, UNCATEGORIZED_DOMAIN
from core.logger import get_logger

logger = get_logger(__name__)


class TableGrouper:
    """
    Classifies tables into business domains using keyword matching against
    table names.

    Usage:
        grouper = TableGrouper(schema)
        groups = grouper.group_tables()
        result = grouper.build_all()
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        rules: Optional[Dict[str, List[str]]] = None,
    ):
        self.schema = schema
        self.tables = schema.get("tables", [])
        self.rules = rules if rules is not None else DOMAIN_RULES

    # ------------------------------------------------------------------
    # SINGLE TABLE CLASSIFICATION
    # ------------------------------------------------------------------

    def classify_table(self, table_name: str) -> str:
        """
        Return the business domain for a single table name based on
        keyword rules. Falls back to UNCATEGORIZED_DOMAIN if nothing
        matches. Matching is case-insensitive and checks for the keyword
        as a substring anywhere in the table name.
        """

        name_lower = table_name.lower()

        for domain, keywords in self.rules.items():
            for keyword in keywords:
                if keyword.lower() in name_lower:
                    return domain

        return UNCATEGORIZED_DOMAIN

    # ------------------------------------------------------------------
    # FULL DATABASE GROUPING
    # ------------------------------------------------------------------

    def group_tables(self) -> Dict[str, List[str]]:
        """
        Classify every table in the schema and return a mapping of
        domain -> list of table names belonging to it.
        """

        logger.info("Grouping %d tables into business domains", len(self.tables))

        groups: Dict[str, List[str]] = defaultdict(list)

        for table in self.tables:
            table_name = table["name"]
            domain = self.classify_table(table_name)
            groups[domain].append(table_name)

        # Ensure deterministic, readable ordering: rule-defined domains
        # first (in rule order), then Uncategorized last.
        ordered_groups: Dict[str, List[str]] = {}
        for domain in self.rules.keys():
            if domain in groups:
                ordered_groups[domain] = sorted(groups[domain])

        if UNCATEGORIZED_DOMAIN in groups:
            ordered_groups[UNCATEGORIZED_DOMAIN] = sorted(groups[UNCATEGORIZED_DOMAIN])

        for domain, tables in ordered_groups.items():
            logger.info("  %s: %d table(s)", domain, len(tables))

        return ordered_groups

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, Any]:
        """
        Run the full Phase 3 grouping and return everything the exporter
        needs:

            {
                "groups": { domain: [table_name, ...] },
                "table_to_domain": { table_name: domain },
                "uncategorized_count": int,
            }
        """

        logger.info("Starting Phase 3 table grouping")

        groups = self.group_tables()

        table_to_domain: Dict[str, str] = {}
        for domain, tables in groups.items():
            for table_name in tables:
                table_to_domain[table_name] = domain

        uncategorized_count = len(groups.get(UNCATEGORIZED_DOMAIN, []))

        if uncategorized_count:
            logger.info(
                "%d table(s) uncategorized — consider updating "
                "config/domain_rules.py: %s",
                uncategorized_count, groups.get(UNCATEGORIZED_DOMAIN, []),
            )

        result = {
            "groups": groups,
            "table_to_domain": table_to_domain,
            "uncategorized_count": uncategorized_count,
        }

        logger.info(
            "Phase 3 complete: %d domain(s), %d table(s) uncategorized",
            len(groups), uncategorized_count,
        )

        return result