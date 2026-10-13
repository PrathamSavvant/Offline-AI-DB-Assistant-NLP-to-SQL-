"""
core/schema_fingerprint.py

Schema Fingerprinting -- fast, deterministic structural hash of the database
schema so main.py can skip phases 2-10 when nothing has changed since the
last crawl.

What IS fingerprinted (structural correctness drivers):
  - Table names
  - Per-table column names, data types, and primary-key flag
  - Foreign key column/table/column triples

What is NOT fingerprinted (would cause spurious mismatches):
  - Row counts (TABLE_ROWS) -- fluctuate every second in a live DB
  - Table/column comments    -- cosmetic, no SQL impact
  - Engine, created_at, updated_at -- metadata, no SQL impact

Usage in the knowledge builder:
    from core import schema_fingerprint

    fingerprint_path = db_context.get_knowledge_dir() / "schema_fingerprint.json"
    current_fp = schema_fingerprint.compute(schema)
    if current_fp == schema_fingerprint.load(fingerprint_path):
        print("Schema unchanged -- skipping phases 2-10")
        return
    # ... run phases 2-10 ...
    schema_fingerprint.save(current_fp, fingerprint_path)
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional

FINGERPRINT_FILE = Path("knowledge") / "schema_fingerprint.json"


def compute(schema: Dict[str, Any]) -> str:
    """
    Produce a deterministic SHA-256 hex digest of the schema's structural
    content.

    All collections are sorted before hashing so that ordering differences
    in INFORMATION_SCHEMA results (which can vary between MySQL versions)
    never produce a spurious "changed" signal.

    Returns a 64-character lowercase hex string.
    """

    tables = schema.get("tables", [])

    canonical = []
    for table in sorted(tables, key=lambda t: t["name"]):
        table_entry: Dict[str, Any] = {
            "name": table["name"],
            "columns": [
                {
                    "name": col["name"],
                    "datatype": col.get("datatype", ""),
                    "full_type": col.get("full_type", ""),
                    "is_primary_key": bool(col.get("is_primary_key", False)),
                }
                for col in sorted(table.get("columns", []), key=lambda c: c["name"])
            ],
            "foreign_keys": [
                {
                    "column": fk["column"],
                    "references_table": fk["references_table"],
                    "references_column": fk["references_column"],
                }
                for fk in sorted(
                    table.get("foreign_keys", []),
                    key=lambda f: f["column"],
                )
            ],
        }
        canonical.append(table_entry)

    # json.dumps with sort_keys=True + compact separators gives a fully
    # deterministic byte sequence regardless of Python dict insertion order.
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_table(table: Dict[str, Any]) -> str:
    """Produce a deterministic SHA-256 hex digest of a single table's structure.

    Uses the same structural signals as compute() but scoped to one table so
    that per-table fingerprints can be stored inside profile files and compared
    during incremental builds.  Only changed tables need their system_knowledge
    refreshed; unchanged tables are left untouched.

    Returns a 64-character lowercase hex string.
    """
    canonical: Dict[str, Any] = {
        "name": table["name"],
        "columns": [
            {
                "name": col["name"],
                "datatype": col.get("datatype", ""),
                "full_type": col.get("full_type", ""),
                "is_primary_key": bool(col.get("is_primary_key", False)),
            }
            for col in sorted(table.get("columns", []), key=lambda c: c["name"])
        ],
        "foreign_keys": [
            {
                "column": fk["column"],
                "references_table": fk["references_table"],
                "references_column": fk["references_column"],
            }
            for fk in sorted(table.get("foreign_keys", []), key=lambda f: f["column"])
        ],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(path: Path = FINGERPRINT_FILE) -> Optional[str]:
    """
    Read the previously saved fingerprint from disk.

    Returns None if the file does not exist yet (first-ever crawl, or the
    knowledge/ directory was wiped), so the caller always does a full build
    on the first run.
    """

    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("fingerprint")
    except (OSError, json.JSONDecodeError):
        # Corrupt or unreadable file: treat as "no fingerprint" so we
        # rebuild rather than silently reuse stale knowledge.
        return None


def save(fingerprint: str, path: Path = FINGERPRINT_FILE) -> None:
    """
    Persist the current fingerprint to disk so the next crawl can compare.
    Creates the knowledge/ directory if it does not exist.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"fingerprint": fingerprint}, fh, indent=2)
