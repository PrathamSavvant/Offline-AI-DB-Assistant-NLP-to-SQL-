"""core/database_registry.py

Database Identity Registry — persistent metadata about a known database.

Persists to knowledge/<DB_NAME>/database.json so that the backend can
recognise a previously-learned database across reconnects.  When the user
reconnects to a database whose knowledge directory already exists, the
registry is loaded and manual knowledge is preserved rather than rebuilt.

Registry schema (database.json):
    {
        "database_id":      "<stable sha256 host/port/schema id>",
        "database_name":    "companydb",
        "fingerprint":      "<sha256 or null>",
        "knowledge_version": "1",
        "created_at":       "<iso8601>",
        "last_scan":        "<iso8601 or null>",
        "last_connected":   "<iso8601 or null>",
        "table_count":      0,
        "profile_count":    0,
        "knowledge_status": "never_scanned"
    }

knowledge_status values:
    "never_scanned"  — directory exists but no scan has completed
    "up_to_date"     — fingerprint matches last scan, profiles intact
    "needs_rebuild"  — fingerprint changed, rebuild pending
    "partial"        — rebuild started but did not complete cleanly
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.db_context import db_context
from core.logger import get_logger

logger = get_logger(__name__)

# Valid knowledge_status values — checked on write so the registry never
# contains an invalid status that confuses the UI.
KNOWLEDGE_STATUSES = frozenset({"never_scanned", "up_to_date", "needs_rebuild", "partial"})

REGISTRY_FILE = "database.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseRegistry:
    """Persistent identity record for the active database.

    The registry resolves its storage path dynamically from
    db_context.get_knowledge_dir() so it always writes into the correct
    per-database namespace, even after a live database switch.

    Usage::

        registry.load()           # read from disk (or create default)
        registry.touch_connected()  # update last_connected timestamp only
        registry.update_after_scan(fp, tables, profiles, "up_to_date")
        info = registry.get()     # dict copy of current in-memory state
    """

    def __init__(self, path: Optional[Path] = None):
        # Allow injection of an explicit path for tests; otherwise resolved
        # from the runtime db_context each time the property is accessed.
        self._custom_path = path
        self._data: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # PATH
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        if self._custom_path is not None:
            return self._custom_path
        return db_context.get_knowledge_dir() / REGISTRY_FILE

    # ------------------------------------------------------------------
    # PERSISTENCE
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """Read the registry from disk.

        If the file does not exist, a new default registry is created and
        persisted immediately so subsequent calls always find a valid file.

        Returns the current in-memory data dict (a copy is available via
        get()).
        """
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._data = raw
                    # Backfill any fields added after the file was written
                    # Older registries used a random UUID.  Preserve it for
                    # auditability, but normalize the active identity to the
                    # deterministic host/port/schema value.
                    if self._data.get("database_id") != db_context.database_id:
                        self._data.setdefault("legacy_database_id", self._data.get("database_id"))
                        self._data["database_id"] = db_context.database_id
                    self._data.setdefault("database_name", db_context.active_database)
                    self._data["connection_identity"] = db_context.connection_identity
                    self._data.setdefault("knowledge_version", "1")
                    self._data.setdefault("knowledge_status", "never_scanned")
                    logger.info(
                        "[Registry] Database recognized: %s (id=%s, status=%s)",
                        self._data["database_name"],
                        self._data["database_id"][:8],
                        self._data["knowledge_status"],
                    )
                    self._save()
                    return self._data
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("[Registry] Could not read database registry: %s — creating new", exc)

        # No file or unreadable: create a fresh registry
        self._data = self._default()
        logger.info(
            "[Registry] No existing registry found for '%s' — creating new (id=%s)",
            self._data["database_name"],
            self._data["database_id"][:8],
        )
        self._save()
        return self._data

    def _default(self) -> Dict[str, Any]:
        return {
            "database_id": db_context.database_id,
            "database_name": db_context.active_database,
            "connection_identity": db_context.connection_identity,
            "fingerprint": None,
            "knowledge_version": "1",
            "created_at": _now(),
            "last_scan": None,
            "last_connected": None,
            "table_count": 0,
            "profile_count": 0,
            "knowledge_status": "never_scanned",
        }

    def _save(self) -> None:
        """Atomically write the current registry to disk."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
            tmp.replace(self.path)
        except Exception as exc:
            logger.warning("[Registry] Could not save database registry: %s", exc)

    # ------------------------------------------------------------------
    # READS
    # ------------------------------------------------------------------

    def get(self) -> Dict[str, Any]:
        """Return a shallow copy of the current registry state."""
        return dict(self._data)

    # ------------------------------------------------------------------
    # WRITES
    # ------------------------------------------------------------------

    def touch_connected(self) -> None:
        """Update only the last_connected timestamp.

        Called on every startup and every database switch so the UI can
        show when the database was most recently accessed.  Safe to call
        even if load() was not called first — skips write if _data is
        empty.
        """
        if not self._data:
            # Registry not yet loaded (e.g. called too early). Silently skip.
            return
        self._data["last_connected"] = _now()
        self._data["database_name"] = db_context.active_database
        self._data["connection_identity"] = db_context.connection_identity
        self._save()
        logger.debug("[Registry] last_connected updated for '%s'", self._data["database_name"])

    def update_after_scan(
        self,
        fingerprint: str,
        table_count: int,
        profile_count: int,
        status: str,
    ) -> None:
        """Update all scan-derived fields after a successful knowledge build.

        Called by main.py after all phases complete successfully.

        Args:
            fingerprint:   Global schema SHA-256 digest.
            table_count:   Number of tables in the scanned schema.
            profile_count: Number of profile files written.
            status:        One of the KNOWLEDGE_STATUSES values.
        """
        if status not in KNOWLEDGE_STATUSES:
            logger.warning("[Registry] Unknown knowledge_status '%s' — defaulting to 'partial'", status)
            status = "partial"

        now = _now()
        self._data.update({
            "fingerprint": fingerprint,
            "last_scan": now,
            "last_connected": now,
            "table_count": table_count,
            "profile_count": profile_count,
            "knowledge_status": status,
            "database_name": db_context.active_database,
            "connection_identity": db_context.connection_identity,
        })
        # Bump the knowledge version counter on each successful scan
        try:
            self._data["knowledge_version"] = str(int(self._data.get("knowledge_version", "0")) + 1)
        except (ValueError, TypeError):
            self._data["knowledge_version"] = "1"

        self._save()
        logger.info(
            "[Registry] Registry updated after scan: %s tables, %s profiles, status=%s, fingerprint=%s...",
            table_count,
            profile_count,
            status,
            fingerprint[:12],
        )

    def reconcile_verified_cache(
        self, fingerprint: str, table_count: int, profile_count: int,
        *, live_verified: bool = False,
    ) -> None:
        """Repair stale registry counters only after artifacts were verified.

        An unchanged live schema check is still a real scan, but it must not
        increment the knowledge version or rebuild approved knowledge.
        """
        if not fingerprint or table_count <= 0 or profile_count != table_count:
            raise ValueError("Cannot reconcile an incomplete schema cache")
        updates = {
            "fingerprint": fingerprint,
            "table_count": table_count,
            "profile_count": profile_count,
            "knowledge_status": "up_to_date",
            "database_name": db_context.active_database,
            "connection_identity": db_context.connection_identity,
        }
        if live_verified:
            updates["last_scan"] = _now()
        if any(self._data.get(key) != value for key, value in updates.items()):
            self._data.update(updates)
            self._save()

    def mark_needs_rebuild(self) -> None:
        """Set status to needs_rebuild without changing other fields."""
        self._data["knowledge_status"] = "needs_rebuild"
        self._save()

    def mark_partial(self) -> None:
        """Set status to partial (rebuild started but did not finish)."""
        self._data["knowledge_status"] = "partial"
        self._save()


# Module-level singleton — mirrors the pattern used by business_dictionary,
# pending_dictionary, etc.  Resolves its path dynamically from db_context so
# it always targets the active database's knowledge directory.
database_registry = DatabaseRegistry()
