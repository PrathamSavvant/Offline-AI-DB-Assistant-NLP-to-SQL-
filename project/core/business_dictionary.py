"""
core/business_dictionary.py

Business Dictionary — persistent abbreviation/expansion/synonym store.

Requirement #6 of the extension: a module the crawler and the semantic
analyzer can both use to translate database shorthand into business
language, e.g.

    UI_ID   -> "User ID"       (abbreviation "UI"  -> expansion "User")
    USR     -> "User"
    PO      -> "Purchase Order"

Each entry looks like:

    {
        "abbreviation": "UI",
        "expansion": "User",
        "synonyms": ["end user", "app user"]
    }

Storage is a plain JSON file (knowledge/business_dictionary.json) — same
pattern as core/table_annotations.py — so no extra services are required
and the file can be inspected/edited by hand if needed. The Flask app
edits this store directly through core/business_dictionary.py, so changes
made from the new "Business Dictionary" frontend page are picked up by
the next question immediately, without restarting the backend.
"""

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)

# The global dictionary deliberately contains language-level abbreviations
# only. Company-specific terms are copied to their owning table profile when
# approved from Pending Dictionary; legacy global entries remain readable so
# existing installations and API consumers continue to work during migration.
GLOBAL_LANGUAGE_TOKENS = {"QTY", "AMT", "DOB", "DEPT", "EMP", "ADDR", "DESC"}

SEED_ENTRIES: Dict[str, Dict[str, Any]] = {
    "QTY": {"expansion": "Quantity", "synonyms": ["amount", "count"]},
    "AMT": {"expansion": "Amount", "synonyms": ["total"]},
    "DOB": {"expansion": "Date Of Birth", "synonyms": ["birthdate", "birth date"]},
    "DEPT": {"expansion": "Department", "synonyms": ["division", "org unit"]},
    "EMP": {"expansion": "Employee", "synonyms": ["staff", "worker"]},
    "ADDR": {"expansion": "Address", "synonyms": []},
    "DESC": {"expansion": "Description", "synonyms": ["details"]},
}


class BusinessDictionaryStore:
    """
    Usage:
        dictionary = BusinessDictionaryStore()
        dictionary.set("UI", "User", synonyms=["end user"])
        dictionary.expand_token("UI_ID")   -> ["User"]
        dictionary.get_all()               -> {abbr: {...}, ...}
    """

    def __init__(self, path: Optional[Path] = None, seed_if_empty: bool = True):
        self._custom_path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.seed_if_empty = seed_if_empty
        self._load()

        if self.seed_if_empty and not self._data:
            for abbr, entry in SEED_ENTRIES.items():
                self._data[abbr] = self._normalise_entry(entry)
            self._save()
            logger.info("Business dictionary seeded with %d default entrie(s)", len(self._data))

    @property
    def path(self) -> Path:
        if self._custom_path is not None:
            return self._custom_path
        return db_context.get_knowledge_dir() / "business_dictionary.json"

    # ------------------------------------------------------------------
    # PERSISTENCE
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                self._data = {str(key).upper(): self._normalise_entry(value) for key, value in raw.items()}
            logger.info("Loaded %d business dictionary entrie(s) from %s", len(self._data), self.path)
        else:
            self._data = {}
            logger.info("No existing business dictionary found at %s (starting empty)", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        temporary.replace(self.path)

    @staticmethod
    def _normalise_list(values: Optional[List[str]]) -> List[str]:
        if isinstance(values, str):
            values = [values]
        return sorted({str(value).strip() for value in (values or []) if str(value).strip()}, key=str.lower)

    @classmethod
    def _normalise_entry(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Accept old JSON entries while adding the richer optional fields."""
        value = value if isinstance(value, dict) else {"expansion": str(value)}
        return {
            "expansion": str(value.get("expansion", "")).strip(),
            "synonyms": cls._normalise_list(value.get("synonyms")),
            "aliases": cls._normalise_list(value.get("aliases")),
            "description": str(value.get("description") or "").strip(),
            "example": str(value.get("example") or "").strip(),
        }

    def reload(self) -> None:
        """Re-read the store from disk for current active database."""
        with self._lock:
            self._load()
            if self.seed_if_empty and not self._data:
                for abbr, entry in SEED_ENTRIES.items():
                    self._data[abbr] = self._normalise_entry(entry)
                self._save()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._data.items()}

    def get(self, abbreviation: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._data.get(abbreviation.upper())
            return dict(entry) if entry else None

    def set(self, abbreviation: str, expansion: str, synonyms: Optional[List[str]] = None,
            aliases: Optional[List[str]] = None, description: str = "", example: str = "") -> Dict[str, Any]:
        key = abbreviation.strip().upper()
        if not key or not expansion.strip():
            raise ValueError("Both abbreviation and expansion are required.")
        entry = self._normalise_entry({
            "expansion": expansion.strip(),
            "synonyms": synonyms,
            "aliases": aliases,
            "description": description,
            "example": example,
        })
        with self._lock:
            self._data[key] = entry
            self._save()
        logger.info("Saved business dictionary entry '%s' -> %s", key, entry)
        return entry

    def add_synonym(self, abbreviation: str, synonym: str) -> Optional[Dict[str, Any]]:
        key = abbreviation.strip().upper()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            synonyms = set(entry.get("synonyms", []))
            synonyms.add(synonym.strip())
            entry["synonyms"] = self._normalise_list(list(synonyms))
            self._save()
            return dict(entry)

    def delete(self, abbreviation: str) -> bool:
        key = abbreviation.strip().upper()
        with self._lock:
            if key not in self._data:
                return False
            del self._data[key]
            self._save()
        logger.info("Deleted business dictionary entry '%s'", key)
        return True

    # ------------------------------------------------------------------
    # LOOKUP / EXPANSION
    # ------------------------------------------------------------------

    def known_abbreviations(self) -> set:
        return set(self._data.keys())

    def language_entries(self) -> Dict[str, Dict[str, Any]]:
        """Return only universal language mappings for new profile-aware code."""
        with self._lock:
            return {key: dict(value) for key, value in self._data.items() if key in GLOBAL_LANGUAGE_TOKENS}

    @staticmethod
    def tokenize(name: str) -> List[str]:
        """
        Split a table/column name into individual tokens, handling both
        snake_case ("UI_ID") and camelCase ("userId") style names.
        """

        # snake_case / kebab-case split first
        parts = re.split(r"[_\-\s]+", name)
        tokens: List[str] = []
        for part in parts:
            if not part:
                continue
            # then split camelCase within each part
            camel_split = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", part)
            tokens.extend(camel_split or [part])
        return [t for t in tokens if t]

    def expand_token(self, token: str) -> Optional[str]:
        """Return the expansion for a single token, if it's a known abbreviation."""
        entry = self._data.get(token.upper())
        return entry["expansion"] if entry else None

    def expand_name(self, name: str) -> Dict[str, Any]:
        """
        Expand every recognizable abbreviation inside a table/column name.

        e.g. expand_name("UI_ID") ->
            {"original": "UI_ID", "expansions": {"UI": "User"}, "expanded": "User ID"}
        """

        tokens = self.tokenize(name)
        expansions: Dict[str, str] = {}
        expanded_tokens: List[str] = []

        for token in tokens:
            expansion = self.expand_token(token)
            if expansion:
                expansions[token.upper()] = expansion
                expanded_tokens.append(expansion)
            else:
                expanded_tokens.append(token)

        return {
            "original": name,
            "expansions": expansions,
            "expanded": " ".join(expanded_tokens),
        }

    def lookup(self, term: str) -> List[str]:
        """
        Reverse lookup: given a free-text term (word or phrase typed by a
        user, e.g. "user"), return every abbreviation whose expansion or
        synonym list matches it — used by the Context Generator's hybrid
        retrieval to map a question's wording back onto real column/table
        abbreviations.
        """

        term_lower = term.strip().lower()
        matches = []
        for abbr, entry in self._data.items():
            phrases = [abbr, entry["expansion"], *entry.get("synonyms", []), *entry.get("aliases", [])]
            if any(re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", term_lower) for phrase in phrases if phrase):
                matches.append(abbr)
        return matches

    def search(self, query: str = "") -> List[Dict[str, Any]]:
        """Search token and all human-facing fields without changing storage."""
        needle = query.strip().lower()
        results = []
        for token, entry in self.get_all().items():
            haystack = " ".join([token, entry["expansion"], entry.get("description", ""), entry.get("example", ""), *entry.get("synonyms", []), *entry.get("aliases", [])]).lower()
            if not needle or needle in haystack:
                results.append({"abbreviation": token, **entry})
        return results

    def resolve_text(self, text: str) -> List[Dict[str, Any]]:
        """Return known business phrases mapped to the database token for prompts."""
        results = []
        for token in self.lookup(text):
            entry = self.get(token)
            if entry:
                results.append({"token": token, "expansion": entry["expansion"], "synonyms": entry.get("synonyms", []), "aliases": entry.get("aliases", [])})
        return results


# Module-level singleton — mirrors the pattern other core modules use for
# shared, always-current state across the crawler and the Flask app.
business_dictionary = BusinessDictionaryStore()
