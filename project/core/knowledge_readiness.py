"""Cheap, read-only checks for the active schema and approved knowledge.

These checks use persisted artifacts and the in-memory cache. They never crawl
MySQL, so health requests stay inexpensive on large databases.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from core import schema_fingerprint
from core.knowledge_files import REQUIRED_KNOWLEDGE_FILES
from config.settings import settings


def inspect_cached_artifacts(directory: Path) -> Dict[str, Any]:
    issues = [f"Missing {name}" for name in REQUIRED_KNOWLEDGE_FILES
              if not (directory / name).is_file()]
    stored = schema_fingerprint.load(directory / "schema_fingerprint.json")
    if not stored:
        issues.append("Missing schema fingerprint")
    schema = {}
    try:
        with (directory / "schema.json").open(encoding="utf-8") as handle:
            schema = json.load(handle)
        tables = schema.get("tables")
        if not isinstance(tables, list) or not tables:
            issues.append("Schema has no tables")
            tables = []
    except (OSError, ValueError, TypeError):
        issues.append("Schema file is unreadable")
        tables = []
    if tables and stored:
        try:
            if schema_fingerprint.compute(schema) != stored:
                issues.append("Cached schema does not match its fingerprint")
        except (KeyError, TypeError, ValueError):
            issues.append("Cached schema has invalid table metadata")
    names = {str(table.get("name") or "") for table in tables if isinstance(table, dict)}
    if "" in names or len(names) != len(tables):
        issues.append("Schema contains missing or duplicate table names")
    profiles = sum((directory / "tables" / f"{name}.json").is_file() for name in names if name)
    if profiles != len(names):
        issues.append(f"Only {profiles} of {len(names)} table profiles are persisted")
    return {
        "valid": not issues,
        "issues": issues,
        "fingerprint": stored,
        "table_count": len(names),
        "profile_count": profiles,
    }


def summarize_readiness(cache, registry, defined_knowledge, sync_status=None,
                        *, include_coverage: bool = True) -> Dict[str, Any]:
    """Report operational readiness separately from semantic coverage."""
    reg = registry.get() if registry else {}
    issues = []
    if not cache.is_loaded():
        issues.append("Schema cache is not loaded")
    elif len(cache.tables) != len(cache.table_profiles):
        issues.append("Not every cached table has a profile")
    if settings.DB_EXPECTED_MIN_TABLES > 0 and len(cache.tables) < settings.DB_EXPECTED_MIN_TABLES:
        issues.append(f"Only {len(cache.tables)} of at least {settings.DB_EXPECTED_MIN_TABLES} expected tables are visible")
    if (reg.get("fingerprint") != getattr(cache, "fingerprint", None)
            or reg.get("table_count") != len(cache.tables)
            or reg.get("profile_count") != len(cache.table_profiles)
            or reg.get("knowledge_status") != "up_to_date"):
        issues.append("Database registry does not match verified cached knowledge")
    sync_status = sync_status or {}
    if sync_status.get("status") == "failed":
        issues.append("Latest live schema synchronization failed")
    if sync_status.get("status") == "rebuilding":
        issues.append("Live schema knowledge is being rebuilt")
    current = sync_status.get("current_fingerprint")
    if current and current != getattr(cache, "fingerprint", None):
        issues.append("Live schema differs from the active cache")

    warnings = []
    if settings.DB_EXPECTED_MIN_TABLES <= 0:
        warnings.append("Expected table count is not configured; metadata visibility cannot be verified")
    if settings.DB_USER.strip().lower() == "root":
        warnings.append("Database account is root; use a dedicated read-only account")
    if not sync_status.get("last_sync_at"):
        warnings.append("No live schema check has been recorded")

    table_names = set(cache.get_table_names()) if cache.is_loaded() else set()
    summary = {
        "schema_ready": not issues,
        "issues": issues,
        "warnings": warnings,
        "tables_cached": len(table_names),
        "profiles_cached": len(cache.table_profiles) if cache.is_loaded() else 0,
        "verified_fk_relationships": cache.get_fk_diagnostics().get("verified_relationship_count", 0) if cache.is_loaded() else 0,
        "last_live_schema_check": sync_status.get("last_sync_at"),
    }
    if include_coverage:
        known = defined_knowledge.all() if defined_knowledge else {}
        defined_count = sum(defined_knowledge.is_defined(entry) for name, entry in known.items()
                            if name in table_names) if defined_knowledge else 0
        meaning_count = sum(bool(str(entry.get("definition") or entry.get("business_name") or "").strip())
                            for name, entry in known.items() if name in table_names)
        summary.update(
            approved_tables=defined_count,
            tables_with_approved_meaning=meaning_count,
            meaning_coverage_percent=round(100 * meaning_count / len(table_names), 1) if table_names else 0.0,
        )
    return summary
