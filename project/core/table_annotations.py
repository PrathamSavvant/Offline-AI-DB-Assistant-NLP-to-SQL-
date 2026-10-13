"""
core/table_annotations.py

Persistent storage for user-provided table meanings, aliases, inclusion
toggles, and priority levels.

Automatic classification (Phase 3's domain_rules.py, Phase 7's semantic
tags) works from keyword lists, which will always miss real-world
abbreviations and shorthand a specific database happens to use — a table
named `dept` means "department" to any human looking at it, but no keyword
rule can know that without being told.

This store lets a human explicitly teach the system:
    dept  -> meaning: "Company departments / organizational units"
             aliases: ["department", "org unit", "division"]
             enabled: True   (include in Phase 9 table selection at all)
             priority: "High"  (Critical / High / Medium / Low)

Once saved, this annotation is treated as authoritative — meaning/aliases
are weighted higher than automatic keyword matches, "enabled" is a hard
filter applied before scoring even begins, and "priority" outweighs the
automatic importance ranking from Phase 4, all in Phase 9's table
selection (core/context_generator.py).

Stored at knowledge/table_annotations.json, completely separate from the
files main.py regenerates on each scan — annotations survive re-running
Phases 1-8 as many times as needed.

BACKWARD COMPATIBILITY: entries written before "enabled"/"priority"
existed only have "meaning" and "aliases". get() fills in the defaults
(enabled=True, priority="Medium") for any entry missing those keys, so
older annotation files keep working without migration.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)

VALID_PRIORITIES = {"Critical", "High", "Medium", "Low"}
DEFAULT_PRIORITY = "Medium"


class TableAnnotationStore:
    """
    Usage:
        store = TableAnnotationStore()
        store.set("dept", "Company departments", ["department"], enabled=True, priority="High")
        store.get("dept")  -> {"meaning": ..., "aliases": [...], "enabled": True, "priority": "High"}
        store.get_all()    -> {table_name: {...}, ...}
    """

    def __init__(self, path: Optional[Path] = None):
        self._custom_path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        if self._custom_path is not None:
            return self._custom_path
        return db_context.get_knowledge_dir() / "table_annotations.json"

    def reload(self) -> None:
        """Re-read table annotations from disk for the current active database."""
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.info("Loaded %d table annotation(s) from %s", len(self._data), self.path)
        else:
            self._data = {}
            logger.info("No existing table annotations found at %s (starting empty)", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Return the live annotations dict. NOTE: this returns the actual
        internal reference, not a copy — callers that want a snapshot
        immune to later set()/delete() calls should copy it themselves.
        """

        return self._data

    def get(self, table_name: str) -> Dict[str, Any]:
        """
        Return the annotation for one table, with defaults filled in for
        any missing field — this is what makes old (pre-enabled/priority)
        entries behave correctly without needing a migration step.
        """

        entry = self._data.get(table_name, {})
        return {
            "meaning": entry.get("meaning", ""),
            "aliases": entry.get("aliases", []),
            "enabled": entry.get("enabled", True),
            "priority": entry.get("priority") or DEFAULT_PRIORITY,
        }

    def set(
        self,
        table_name: str,
        meaning: str,
        aliases: List[str],
        enabled: bool = True,
        priority: str = DEFAULT_PRIORITY,
    ) -> Dict[str, Any]:
        cleaned_aliases = sorted({a.strip() for a in aliases if a.strip()})

        priority_clean = priority if priority in VALID_PRIORITIES else DEFAULT_PRIORITY
        if priority not in VALID_PRIORITIES:
            logger.warning(
                "Unknown priority '%s' for table '%s' — defaulting to '%s'",
                priority, table_name, DEFAULT_PRIORITY,
            )

        entry = {
            "enabled": bool(enabled),
            "priority": priority_clean,
            "meaning": meaning.strip(),
            "aliases": cleaned_aliases,
        }
        self._data[table_name] = entry
        self._save()
        logger.info("Saved annotation for table '%s': %s", table_name, entry)
        return entry

    def delete(self, table_name: str) -> None:
        if table_name in self._data:
            del self._data[table_name]
            self._save()
            logger.info("Deleted annotation for table '%s'", table_name)