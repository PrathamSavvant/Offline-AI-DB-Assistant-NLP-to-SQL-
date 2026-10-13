"""
core/db_context.py

Runtime Database Context — Single Source of Truth for the active database
and knowledge directory resolution. Keeps runtime database state cleanly separated
from static connection configuration.
"""

from pathlib import Path
import hashlib
import shutil
from typing import Any, Callable, Dict, List, Optional
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class RuntimeDatabaseContext:
    """
    Manages the active database context at runtime.
    Services query db_context.active_database or db_context.get_knowledge_dir()
    instead of storing their own copies of database names or hardcoded paths.
    """

    def __init__(self):
        self._active_database: str = settings.DB_NAME
        self._listeners: List[Callable[[str], None]] = []

    @property
    def active_database(self) -> str:
        return self._active_database

    @property
    def connection_identity(self) -> str:
        """Stable identity for a logical MySQL schema, never a session id."""
        return "mysql://%s:%s/%s" % (
            (settings.DB_HOST or "localhost").strip().lower(),
            int(settings.DB_PORT or 3306),
            self._active_database.strip().lower(),
        )

    @property
    def database_id(self) -> str:
        """Deterministic, filesystem-safe id used for the knowledge namespace."""
        return hashlib.sha256(self.connection_identity.encode("utf-8")).hexdigest()[:16]

    def get_knowledge_dir(self) -> Path:
        """
        Returns the per-database namespaced knowledge directory path.
        e.g., project/knowledge/CompanyDB or project/knowledge/UniversityDB
        """
        # A rebuild subprocess may target a staging directory.  This is an
        # explicit one-process override, never consulted by request handlers.
        import os
        staged = os.getenv("KNOWLEDGE_OUTPUT_DIR")
        if staged:
            return Path(staged).resolve()
        base_dir = Path(__file__).resolve().parent.parent / "knowledge"
        # The old <database-name> namespace cannot distinguish two servers
        # hosting the same schema.  Keep a readable prefix, but include a
        # deterministic connection identity suffix.
        legacy_dir = base_dir / re_sub_dbname(self._active_database)
        target = base_dir / f"{re_sub_dbname(self._active_database)}--{self.database_id}"
        if not target.exists() and legacy_dir.exists():
            # One-time, non-destructive migration.  Copying first ensures a
            # bad/incomplete legacy directory is never lost.
            try:
                shutil.copytree(legacy_dir, target)
                logger.info("Migrated legacy knowledge namespace %s -> %s", legacy_dir.name, target.name)
            except FileExistsError:
                pass
            except OSError as exc:
                logger.warning("Could not migrate legacy knowledge namespace %s: %s", legacy_dir, exc)
        return target

    def register_listener(self, callback: Callable[[str], None]) -> None:
        """Register a callback to be notified when the active database changes."""
        self._listeners.append(callback)

    def set_active_database(
        self,
        dbname: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        """
        Update the active database context and synchronize configuration settings.
        """
        old_db = self._active_database
        new_db = dbname.strip()
        if not new_db:
            raise ValueError("Database name cannot be empty.")

        self._active_database = new_db

        # Synchronize settings
        settings.DB_NAME = new_db
        if host:
            settings.DB_HOST = host.strip()
        if port:
            settings.DB_PORT = port
        if user:
            settings.DB_USER = user.strip()
        if password is not None:
            settings.DB_PASSWORD = password

        logger.info("Runtime database context switched: '%s' -> '%s'", old_db, new_db)

        # Notify listeners
        for listener in self._listeners:
            try:
                listener(new_db)
            except Exception as exc:
                logger.error("Error in db_context switch listener: %s", exc)


def re_sub_dbname(dbname: str) -> str:
    import re
    cleaned = re.sub(r"[^\w\-]", "_", dbname)
    return cleaned or "default"


# Global singleton instance for runtime database context
db_context = RuntimeDatabaseContext()
