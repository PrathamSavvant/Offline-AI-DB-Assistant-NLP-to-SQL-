"""Persistent, deterministic table knowledge profiles.

Profiles are built during the crawl/rebuild lifecycle, not on user requests.
They keep the runtime's business understanding of a table beside its compact
structural metadata so retrieval and planners can work from durable knowledge.

Profile v2 splits each profile into two independent sections:

  manual_knowledge  (User-owned)
    - definition    : human description of the table
    - synonyms      : user-approved alternate names
    - abbreviations : user-approved token→meaning mappings, each with a status
                      field ("ok" | "needs_review") so the UI can surface
                      abbreviations whose referenced column was deleted.
    This section is NEVER overwritten by the crawler.  It is only changed when
    the user edits it via the UI or approves pending knowledge.

  system_knowledge  (Backend-owned)
    - columns, primary_key, foreign_keys, relationships, statistics,
      domain, business_tags, is_lookup_table, last_scan
    This section is rebuilt on every scan where the table's per-table
    fingerprint changes.

All existing top-level fields (aliases, abbreviations, planner_hints, …)
are kept at the profile root for full backward compatibility with every
existing runtime component (ContextGenerator, KnowledgeSearchEngine, etc.).
Those components never need to know about the v2 internal structure.

Migration from v1:
  TableKnowledgeRepository.load_all() automatically upgrades v1 profiles to
  v2 in-place on first load.  Zero data is lost.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from core import schema_fingerprint
from core.logger import get_logger

logger = get_logger(__name__)
PROFILE_VERSION = 2

_IDENTITY_TERMS = {"id", "identifier", "number", "no", "code", "key"}
_DISPLAY_TERMS = {"name", "title", "label", "description", "summary"}
_LOOKUP_TERMS = {"group", "type", "class", "category", "department", "unit", "status"}
_FILTER_TERMS = _LOOKUP_TERMS | {"state", "active", "enabled", "date", "created", "updated"}
_SORT_TERMS = {"date", "created", "updated", "name", "title", "status", "priority"}


def _words(value: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9]+", str(value).replace("_", " ").lower())


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


# ---------------------------------------------------------------------------
# v1 → v2 migration helper (module-level so it can be tested independently)
# ---------------------------------------------------------------------------

def _migrate_v1_to_v2(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Non-destructive in-memory migration from profile v1 to v2.

    Wraps the flat v1 structure into the new manual_knowledge /
    system_knowledge sections while preserving all existing top-level
    fields so no downstream reader breaks.

    This function is idempotent: calling it on a v2 profile is harmless
    (version check in load_all prevents that, but the guard is cheap).
    """
    profile = dict(profile)  # shallow copy — do not mutate caller's dict

    # Already v2
    if profile.get("profile_version", 1) == PROFILE_VERSION:
        return profile

    # -- Lift abbreviations into manual_knowledge --------------------------
    existing_abbrs: Dict[str, Any] = profile.get("abbreviations", {})
    manual_abbrs: Dict[str, Any] = {}
    for token, entry in existing_abbrs.items():
        token = str(token).upper()
        if isinstance(entry, dict):
            manual_abbrs[token] = {
                "abbreviation": entry.get("abbreviation", token),
                "meaning": str(entry.get("expansion", entry.get("meaning", ""))).strip(),
                "column": str(entry.get("column", entry.get("maps_to_column", ""))).strip(),
                "status": "ok",
            }
        else:
            manual_abbrs[token] = {
                "abbreviation": token,
                "meaning": str(entry).strip(),
                "column": "",
                "status": "ok",
            }

    manual_knowledge = {
        "definition": str(profile.get("purpose") or profile.get("description") or "").strip(),
        # Synonyms are stored in defined_knowledge.json, not in v1 profiles.
        "synonyms": [],
        "abbreviations": manual_abbrs,
        "last_edited": None,
    }

    # -- Lift structural data into system_knowledge ------------------------
    schema_block = profile.get("schema", {})
    stats = profile.get("statistics", {})
    hints = profile.get("planner_hints", {})

    system_knowledge = {
        "columns": schema_block.get("columns", []),
        "primary_key": schema_block.get("primary_key", profile.get("identity_columns", [])),
        "foreign_keys": schema_block.get("foreign_keys", []),
        "relationships": profile.get("relationships", []),
        "statistics": stats,
        "importance_score": stats.get("importance_score"),
        "importance_rank": stats.get("importance_rank"),
        "domain": hints.get("domain", ""),
        "business_tags": hints.get("business_tags", []),
        "is_lookup_table": hints.get("is_lookup_table", False),
        "last_scan": profile.get("generated_at"),
    }

    profile["profile_version"] = PROFILE_VERSION
    # Per-table fingerprint is unknown for migrated profiles; the next scan
    # will compute and store it, causing a one-time full system_knowledge refresh.
    profile["table_fingerprint"] = ""
    profile["manual_knowledge"] = manual_knowledge
    profile["system_knowledge"] = system_knowledge
    # Top-level fields are preserved as-is.

    logger.info(
        "[Migration] v1→v2: table '%s' migrated (%d abbreviation(s))",
        profile.get("table", "unknown"),
        len(manual_abbrs),
    )
    return profile


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class TableKnowledgeBuilder:
    """Build complete profiles from already-crawled knowledge artifacts.

    The builder never queries the database or calls an LLM.  Keeping these
    derivations here ensures a request only reads precomputed table knowledge.
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        semantics: Optional[Dict[str, Any]] = None,
        relationships: Optional[Dict[str, Any]] = None,
        grouping: Optional[Dict[str, Any]] = None,
        ranking: Optional[Dict[str, Any]] = None,
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        dictionary: Any = None,
    ):
        self.schema = schema
        self.semantics = (semantics or {}).get("tables", semantics or {})
        self.relationships = (relationships or {}).get("relationships", [])
        self.grouping = grouping or {}
        self.ranking = {item.get("table"): item for item in (ranking or {}).get("rankings", [])}
        self.annotations = annotations or {}
        self.dictionary = dictionary

    # ------------------------------------------------------------------
    # PRIVATE HELPERS
    # ------------------------------------------------------------------

    def _columns_with_terms(self, table: Dict[str, Any], terms: set) -> List[str]:
        return [
            column["name"] for column in table.get("columns", [])
            if terms & set(_words(column.get("name", "")))
        ]

    def _relationships(self, table_name: str) -> List[Dict[str, str]]:
        result = []
        for relation in self.relationships:
            if relation.get("from_table") == table_name:
                result.append({
                    "column": relation.get("from_column", ""),
                    "related_table": relation.get("to_table", ""),
                    "related_column": relation.get("to_column", ""),
                    "direction": "outbound",
                })
            elif relation.get("to_table") == table_name:
                result.append({
                    "column": relation.get("to_column", ""),
                    "related_table": relation.get("from_table", ""),
                    "related_column": relation.get("from_column", ""),
                    "direction": "inbound",
                })
        return result

    def _auto_abbreviations(self, table: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """Discover abbreviations from the business dictionary for this table."""
        if self.dictionary is None:
            return {}
        present = {
            token.upper()
            for value in [table.get("name", "")] + [c.get("name", "") for c in table.get("columns", [])]
            for token in self.dictionary.tokenize(value)
        }
        result: Dict[str, Dict[str, Any]] = {}
        for token in sorted(present):
            if hasattr(self.dictionary, "language_entries") and token in self.dictionary.language_entries():
                continue
            entry = self.dictionary.get(token)
            if entry:
                result[token] = {
                    "abbreviation": token,
                    "meaning": entry.get("expansion", ""),
                    "column": "",
                    "status": "ok",
                }
        return result

    @staticmethod
    def _check_column_removal(
        abbreviations: Dict[str, Any],
        current_column_names: Set[str],
    ) -> Dict[str, Any]:
        """Mark abbreviations whose referenced column was deleted.

        Sets status="needs_review" and adds an issue message.
        Clears the needs_review status (and issue) if the column now exists.
        Abbreviations with no column reference are left unchanged.

        The dict is modified in-place and returned.
        """
        for token, entry in abbreviations.items():
            if not isinstance(entry, dict):
                continue
            col_ref = entry.get("column", "")
            if not col_ref:
                # No column reference — not subject to removal detection.
                entry.pop("issue", None)
                entry.setdefault("status", "ok")
                continue
            if col_ref not in current_column_names:
                # Column was deleted.
                if entry.get("status") != "needs_review":
                    logger.warning(
                        "[Profile] Abbreviation '%s' references deleted column '%s' — marked needs_review",
                        token, col_ref,
                    )
                entry["status"] = "needs_review"
                entry["issue"] = f"Referenced column '{col_ref}' no longer exists"
            else:
                # Column still exists: clear any previous warning.
                entry.pop("issue", None)
                entry["status"] = "ok"
        return abbreviations

    # ------------------------------------------------------------------
    # PUBLIC BUILD METHODS
    # ------------------------------------------------------------------

    def build_profile(
        self,
        table: Dict[str, Any],
        existing: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a complete v2 profile for *table*.

        Preserves manual_knowledge from *existing* (never overwrites user
        knowledge).  Rebuilds system_knowledge from fresh scan artifacts.
        Detects column removal and marks affected abbreviations as
        needs_review.
        """
        name = table["name"]

        # -- 1. Extract (or initialise) manual_knowledge -------------------
        existing_v2 = existing
        if existing and existing.get("profile_version", 1) < PROFILE_VERSION:
            existing_v2 = _migrate_v1_to_v2(existing)

        if existing_v2 and "manual_knowledge" in existing_v2:
            manual_knowledge: Dict[str, Any] = {
                "definition": existing_v2["manual_knowledge"].get("definition", ""),
                "synonyms": list(existing_v2["manual_knowledge"].get("synonyms", [])),
                "abbreviations": dict(existing_v2["manual_knowledge"].get("abbreviations", {})),
                "last_edited": existing_v2["manual_knowledge"].get("last_edited"),
            }
        else:
            manual_knowledge = {
                "definition": "",
                "synonyms": [],
                "abbreviations": {},
                "last_edited": None,
            }

        # Automatically detected abbreviations are pending suggestions, not
        # manual knowledge.  Never promote crawler output into this section.

        # -- 2. Column removal detection -----------------------------------
        current_column_names: Set[str] = {c["name"] for c in table.get("columns", [])}
        self._check_column_removal(manual_knowledge["abbreviations"], current_column_names)

        # Active abbreviations = those not marked needs_review
        active_abbrs = {
            token: entry
            for token, entry in manual_knowledge["abbreviations"].items()
            if isinstance(entry, dict) and entry.get("status") != "needs_review"
        }

        # -- 3. Build system_knowledge from fresh scan artifacts -----------
        semantic = self.semantics.get(name, {})
        annotation = self.annotations.get(name, {})
        columns = table.get("columns", [])
        names = [column.get("name", "") for column in columns]
        primary_key = _unique([
            *table.get("primary_key", []),
            *(column["name"] for column in columns if column.get("is_primary_key")),
        ])
        tags = semantic.get("column_semantics", {})
        identity = _unique([
            *primary_key,
            *(column for column, value in tags.items() if "identifier" in value),
            *self._columns_with_terms(table, _IDENTITY_TERMS),
        ])[:4]
        display = [column for column, value in tags.items() if "descriptive" in value and column not in identity]
        display = _unique([
            *display,
            *(column for column in self._columns_with_terms(table, _DISPLAY_TERMS) if column not in identity),
        ])[:4]
        business = (
            _unique(column for column, value in tags.items() if value and column not in identity and column not in display)
            or list((existing_v2 or {}).get("business_columns", []))
        )
        lookup = self._columns_with_terms(table, _LOOKUP_TERMS)
        relationships = self._relationships(name) or list((existing_v2 or {}).get("relationships", []))

        domain = self.grouping.get("table_to_domain", {}).get(
            name,
            semantic.get("domain", (existing_v2 or {}).get("planner_hints", {}).get("domain", "")),
        )
        business_tags = semantic.get("business_tags", []) or (existing_v2 or {}).get("planner_hints", {}).get("business_tags", [])
        is_lookup = bool(lookup and len(columns) <= 8)
        importance_score = self.ranking.get(name, {}).get(
            "score", (existing_v2 or {}).get("statistics", {}).get("importance_score"),
        )
        importance_rank = self.ranking.get(name, {}).get(
            "rank", (existing_v2 or {}).get("statistics", {}).get("importance_rank"),
        )

        now = datetime.now(timezone.utc).isoformat()

        system_knowledge: Dict[str, Any] = {
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": table.get("foreign_keys", []),
            "relationships": relationships,
            "statistics": {
                "approx_row_count": table.get("approx_row_count"),
                "column_count": len(columns),
                "importance_score": importance_score,
                "importance_rank": importance_rank,
            },
            "importance_score": importance_score,
            "importance_rank": importance_rank,
            "domain": domain,
            "business_tags": business_tags,
            "is_lookup_table": is_lookup,
            "last_scan": now,
        }

        # -- 4. Per-table fingerprint --------------------------------------
        table_fp = schema_fingerprint.compute_table(table)

        # -- 5. Derive top-level fields (backward compat) ------------------
        readable = " ".join(_words(name)).title()
        aliases = _unique([
            readable, name.replace("_", " "), name.rstrip("s").replace("_", " "),
            *annotation.get("aliases", []),
            *business_tags,
            *(existing_v2 or {}).get("aliases", []),
        ])
        purpose = (
            annotation.get("meaning")
            or manual_knowledge["definition"]
            or semantic.get("summary")
            or (existing_v2 or {}).get("purpose")
            or f"Stores {name.replace('_', ' ')} records."
        )

        profile: Dict[str, Any] = {
            "profile_version": PROFILE_VERSION,
            "table": name,
            "table_fingerprint": table_fp,

            # Sectioned storage (v2 architecture)
            "manual_knowledge": manual_knowledge,
            "system_knowledge": system_knowledge,

            # ---------------------------------------------------------------
            # Top-level flat fields — unchanged from v1 for full backward
            # compatibility with ContextGenerator, KnowledgeSearchEngine,
            # SemanticContextBuilder, PresentationContextBuilder, etc.
            # These are derived from manual_knowledge + system_knowledge and
            # re-computed each time the profile is rebuilt.
            # ---------------------------------------------------------------
            "business_name": annotation.get("meaning") or readable,
            "description": purpose,
            "purpose": purpose,
            "aliases": aliases,
            # Active abbreviations only (status=ok).  needs_review entries are
            # visible in the UI but excluded from the runtime search index.
            "abbreviations": active_abbrs,
            "identity_columns": identity,
            "display_columns": display,
            "business_columns": business,
            "lookup_columns": lookup,
            "commonly_filtered_columns": self._columns_with_terms(table, _FILTER_TERMS),
            "commonly_sorted_columns": self._columns_with_terms(table, _SORT_TERMS),
            "relationships": relationships,
            "related_tables": _unique(item["related_table"] for item in relationships),
            "common_queries": list((existing_v2 or {}).get("common_queries", [])),
            "planner_hints": {
                "domain": domain,
                "business_tags": business_tags,
                "primary_key": primary_key,
                "is_lookup_table": is_lookup,
            },
            "statistics": {
                "approx_row_count": table.get("approx_row_count"),
                "column_count": len(columns),
                "importance_score": importance_score,
                "importance_rank": importance_rank,
            },
            "schema": {
                "type": table.get("type"),
                "columns": columns,
                "primary_key": primary_key,
                "foreign_keys": table.get("foreign_keys", []),
            },
            "generated_at": now,
        }
        return profile

    def build_all(
        self,
        existing_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        changed_tables: Optional[Set[str]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        """Build profiles for all tables.

        Args:
            existing_profiles: Previously persisted profiles (may be v1 or v2).
                               Used to preserve manual_knowledge.
            changed_tables:    Set of table names whose per-table fingerprint
                               differs from the stored value.  When provided,
                               only these tables have their system_knowledge
                               rebuilt; all other tables are returned as-is
                               from *existing_profiles*.
                               When None (--force or first build), every table
                               is fully rebuilt.
        """
        existing_profiles = existing_profiles or {}
        profiles: Dict[str, Dict[str, Any]] = {}

        rebuilt = 0
        skipped = 0

        for table in self.schema.get("tables", []):
            tname = table["name"]
            existing = existing_profiles.get(tname)

            if changed_tables is not None and tname not in changed_tables and existing is not None:
                # Table unchanged — pass existing profile through.
                # Migrate v1→v2 if needed so the repository always writes v2.
                if existing.get("profile_version", 1) < PROFILE_VERSION:
                    existing = _migrate_v1_to_v2(existing)
                profiles[tname] = existing
                skipped += 1
            else:
                profiles[tname] = self.build_profile(table, existing)
                rebuilt += 1

        if changed_tables is not None:
            logger.info(
                "[Profiles] System knowledge refreshed for %d table(s); %d table(s) unchanged — skipped",
                rebuilt,
                skipped,
            )
            logger.info("[Profiles] Manual knowledge preserved for all %d table(s)", len(profiles))
        else:
            logger.info("[Profiles] Full profile build completed for %d table(s)", len(profiles))

        return profiles


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class TableKnowledgeRepository:
    """Loads and persists profile files; v1 profiles are upgraded automatically."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all profiles from disk.

        v1 profiles are migrated to v2 in-place (saved back to disk) so that
        the next incremental build finds v2 files and correctly identifies
        unchanged tables.
        """
        profiles: Dict[str, Dict[str, Any]] = {}
        if not self.directory.exists():
            return profiles

        migrated_count = 0
        for path in sorted(self.directory.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)

                if not data.get("table"):
                    continue  # malformed file — skip

                version = data.get("profile_version", 1)

                if version == PROFILE_VERSION:
                    profiles[data["table"]] = data

                elif version == 1:
                    # Automatic migration: v1 → v2 in-place.
                    migrated = _migrate_v1_to_v2(data)
                    self.save_profile(migrated["table"], migrated)
                    profiles[migrated["table"]] = migrated
                    migrated_count += 1

                # Other version numbers: skip (future forward-compat guard).

            except (OSError, ValueError, AttributeError) as exc:
                logger.warning("Could not read table knowledge profile %s: %s", path, exc)

        if migrated_count:
            logger.info(
                "[Migration] Migrated %d table profile(s) from v1 to v2 (manual knowledge preserved)",
                migrated_count,
            )

        return profiles

    def save_all(self, profiles: Dict[str, Dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # Remove only recognised, generated profiles that no longer belong to
        # the current schema.  This prevents stale deleted tables from being
        # searchable after a successful schema rebuild.
        for path in self.directory.glob("*.json"):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    previous = json.load(handle)
                # Accept both v1 and v2 when checking for stale files
                if previous.get("profile_version") in {1, PROFILE_VERSION} and previous.get("table") not in profiles:
                    path.unlink()
            except (OSError, ValueError, AttributeError):
                continue
        for table_name, profile in profiles.items():
            self.save_profile(table_name, profile)
        logger.info("Persisted %d table knowledge profile(s) in %s", len(profiles), self.directory)

    def save_profile(self, table_name: str, profile: Dict[str, Any]) -> None:
        """Atomically update one profile without touching sibling tables."""
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{table_name}.json"
        temporary = target.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2, ensure_ascii=False, default=str)
        temporary.replace(target)
