"""
core/pending_dictionary.py

Pending Dictionary — holding area for abbreviations the crawler finds in
table/column names that are NOT yet in the Business Dictionary
(core/business_dictionary.py).

Requirement #10 of the extension:

    XYZ_ACC  ->  unknown abbreviation "XYZ" found, no entry for it yet
             ->  stored here so an admin can assign its meaning from the
                 new "Pending Dictionary" frontend page.

Once an admin resolves an entry, it's written into the Business
Dictionary and removed from this store.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from core.business_dictionary import BusinessDictionaryStore
from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)

# Tokens that are structurally "abbreviation-shaped" (short, uppercase-ish)
# but are common enough / self-explanatory enough that flagging them would
# just be noise.
IGNORE_TOKENS: Set[str] = {
    "ID", "AT", "BY", "ON", "IS", "NO", "OF", "IN", "TO", "PK", "FK",
    "URL", "URI", "IP", "UUID", "GUID", "JSON", "XML", "HTML", "CSV",
}

# Only tokens up to this length are considered candidate abbreviations —
# longer tokens are almost always already-readable words.
MAX_ABBREVIATION_LENGTH = 5

# Tokens this short (<=3 chars) are flagged regardless of casing, since
# almost nothing that short is a real English word in a schema name
# ("PO", "UI", "acc"). Longer candidates (4-5 chars) are only flagged if
# they were written in ALL CAPS in the source name (e.g. "USR", "DEPT")
# — an ordinary lowercase word of that length ("users", "email") is
# almost always already a readable word, not an abbreviation.
ALWAYS_FLAG_LENGTH = 3

# These are intentionally not auto-expanded: their meaning may vary by
# company. They are surfaced as suggestions for a human to review.
MEDIUM_CONFIDENCE_TOKENS = {"ACC", "NUM", "TEL", "ZIP"}


class PendingDictionaryStore:
    """
    Usage:
        pending = PendingDictionaryStore()
        pending.scan_schema(schema)              # populated by the crawler
        pending.get_all()                        -> {token: {...}, ...}
        pending.resolve("XYZ", "External System", dictionary_store)
    """

    def __init__(self, path: Optional[Path] = None):
        self._custom_path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        if self._custom_path is not None:
            return self._custom_path
        return db_context.get_knowledge_dir() / "pending_dictionary.json"

    def reload(self) -> None:
        """Re-read pending dictionary from disk for the current active database."""
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        return self._data

    @staticmethod
    def _looks_like_abbreviation(token: str) -> bool:
        if not token or len(token) > MAX_ABBREVIATION_LENGTH:
            return False
        if token.upper() in IGNORE_TOKENS:
            return False
        if not token.isalpha():
            return False

        if len(token) <= ALWAYS_FLAG_LENGTH:
            return True

        # 4-5 char tokens: only flag if written in ALL CAPS in the source
        # name — a lowercase word that length ("users", "email") is
        # almost always already readable, not shorthand.
        return token.isupper()

    def scan_schema(self, schema: Dict[str, Any], dictionary: BusinessDictionaryStore) -> List[str]:
        """
        Walk every table/column name in a scanned schema, and record any
        abbreviation-shaped token that ISN'T already in the Business
        Dictionary. Called by the crawler (core/scanner.py) after every
        scan. Returns the list of newly-seen tokens.
        """

        known = dictionary.known_abbreviations()
        newly_found: List[str] = []

        for table in schema.get("tables", []):
            self._scan_name(table["name"], "table", table["name"], known, newly_found, dictionary)
            for column in table.get("columns", []):
                self._scan_name(
                    column["name"], "column", table["name"], known, newly_found, dictionary,
                    column_name=column["name"],
                )

        if newly_found:
            self._save()
            logger.info("Pending dictionary: %d new unknown abbreviation(s) found", len(newly_found))

        return newly_found

    def _scan_name(
        self,
        name: str,
        source_type: str,
        table_name: str,
        known: Set[str],
        newly_found: List[str],
        dictionary: BusinessDictionaryStore,
        column_name: str = None,
    ) -> None:
        for token in dictionary.tokenize(name):
            token_upper = token.upper()
            if token_upper in known or not self._looks_like_abbreviation(token):
                continue

            if token_upper not in self._data:
                self._data[token_upper] = {
                    "token": token_upper,
                    "example_source": f"{table_name}.{column_name}" if column_name else table_name,
                    "occurrences": [],
                    "table": table_name,
                    "column": column_name,
                    "where_found": source_type,
                    "sample_values": [],  # Schema scans do not read user data.
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "classification": "suggestion" if token_upper in MEDIUM_CONFIDENCE_TOKENS else "pending",
                    "confidence": "medium" if token_upper in MEDIUM_CONFIDENCE_TOKENS else "low",
                }
                newly_found.append(token_upper)

            example = f"{table_name}.{column_name}" if column_name else table_name
            occurrences = self._data[token_upper].setdefault("occurrences", [])
            if example not in occurrences:
                occurrences.append(example)

    def resolve(
        self,
        token: str,
        expansion: str,
        dictionary: BusinessDictionaryStore,
        synonyms: List[str] = None,
        aliases: List[str] = None,
        description: str = "",
        example: str = "",
    ) -> Dict[str, Any]:
        """
        Assign a meaning to a pending abbreviation: writes it into the
        Business Dictionary and removes it from the pending list.
        """

        key = token.strip().upper()
        entry = dictionary.set(
            key, expansion, synonyms=synonyms, aliases=aliases,
            description=description, example=example,
        )

        if key in self._data:
            del self._data[key]
            self._save()

        logger.info("Resolved pending abbreviation '%s' -> '%s'", key, expansion)
        return entry

    def dismiss(self, token: str) -> None:
        """Remove a pending entry without adding it to the dictionary."""
        key = token.strip().upper()
        if key in self._data:
            del self._data[key]
            self._save()


pending_dictionary = PendingDictionaryStore()
