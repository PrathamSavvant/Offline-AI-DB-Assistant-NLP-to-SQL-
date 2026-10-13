"""
core/schema_differ.py

Schema Change Detection — compares the previous crawl's schema snapshot
against the current one and reports what changed.

Requirement #9 of the extension. Runs on every crawler execution
(core/scanner.py DatabaseScanner.crawl()), comparing:

    knowledge/schema.json (previous crawl, read before it's overwritten)
    vs.
    the freshly-scanned schema dict (current crawl)

and detects:
    - new tables
    - removed tables
    - new columns
    - removed columns

The diff is persisted to knowledge/schema_changes.json (most recent diff)
and appended to knowledge/schema_changes_history.json (bounded history),
which is what the new "Schema Changes" frontend page reads.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)

MAX_HISTORY_ENTRIES = 50


class SchemaDiffer:
    """
    Usage:
        differ = SchemaDiffer()
        diff = differ.compare(previous_schema, current_schema)
        differ.persist(diff)
    """

    @property
    def latest_path(self) -> Path:
        return db_context.get_knowledge_dir() / "schema_changes.json"

    @property
    def history_path(self) -> Path:
        return db_context.get_knowledge_dir() / "schema_changes_history.json"

    @staticmethod
    def _columns_by_table(schema: Dict[str, Any]) -> Dict[str, set]:
        result = {}
        for table in schema.get("tables", []):
            result[table["name"]] = {c["name"] for c in table.get("columns", [])}
        return result

    def compare(self, previous_schema: Dict[str, Any], current_schema: Dict[str, Any]) -> Dict[str, Any]:
        prev_tables = {t["name"] for t in previous_schema.get("tables", [])} if previous_schema else set()
        curr_tables = {t["name"] for t in current_schema.get("tables", [])}

        added_tables = sorted(curr_tables - prev_tables)
        removed_tables = sorted(prev_tables - curr_tables)

        prev_columns = self._columns_by_table(previous_schema) if previous_schema else {}
        curr_columns = self._columns_by_table(current_schema)

        added_columns: List[Dict[str, Any]] = []
        removed_columns: List[Dict[str, Any]] = []

        # Only compare columns for tables that existed in both snapshots —
        # a brand-new table's columns are implied by "added_tables", not
        # reported again as individual column additions.
        for table_name in curr_tables & prev_tables:
            prev_cols = prev_columns.get(table_name, set())
            curr_cols = curr_columns.get(table_name, set())

            for col in sorted(curr_cols - prev_cols):
                added_columns.append({"table": table_name, "column": col})
            for col in sorted(prev_cols - curr_cols):
                removed_columns.append({"table": table_name, "column": col})

        has_changes = bool(added_tables or removed_tables or added_columns or removed_columns)

        diff = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "has_changes": has_changes,
            "added_tables": added_tables,
            "removed_tables": removed_tables,
            "added_columns": added_columns,
            "removed_columns": removed_columns,
        }

        if has_changes:
            logger.info(
                "Schema changes detected: +%d table(s), -%d table(s), +%d column(s), -%d column(s)",
                len(added_tables), len(removed_tables), len(added_columns), len(removed_columns),
            )
        else:
            logger.info("Schema diff: no changes detected since last crawl")

        return diff

    def persist(self, diff: Dict[str, Any]) -> None:
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.latest_path, "w", encoding="utf-8") as f:
            json.dump(diff, f, indent=2, ensure_ascii=False)

        history: List[Dict[str, Any]] = []
        if self.history_path.exists():
            try:
                with open(self.history_path, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except (json.JSONDecodeError, OSError):
                history = []

        history.append(diff)
        history = history[-MAX_HISTORY_ENTRIES:]

        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    @staticmethod
    def load_previous_schema(schema_path: Optional[Path] = None) -> Dict[str, Any]:
        """Read the schema snapshot from the PREVIOUS crawl, before it's overwritten."""
        path = schema_path if schema_path is not None else (db_context.get_knowledge_dir() / "schema.json")
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def load_latest_diff() -> Dict[str, Any]:
        path = db_context.get_knowledge_dir() / "schema_changes.json"
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def load_history() -> List[Dict[str, Any]]:
        path = db_context.get_knowledge_dir() / "schema_changes_history.json"
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
