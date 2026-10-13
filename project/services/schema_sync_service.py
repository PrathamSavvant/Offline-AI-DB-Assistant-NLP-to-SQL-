"""Independent, lock-protected background schema synchronization service."""
from __future__ import annotations

import inspect
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from config.settings import settings
from core import schema_fingerprint
from core.database import Database
from core.db_context import db_context
from core.logger import get_logger
from core.knowledge_files import LEGACY_KNOWLEDGE_FILE_ALIASES, REQUIRED_KNOWLEDGE_FILES
from core.database_registry import database_registry
from core.scanner import DatabaseScanner
from core.knowledge_readiness import inspect_cached_artifacts

logger = get_logger(__name__)


def _safe_rmtree(path: Path) -> None:
    """Delete a directory tree, retrying on Windows file-lock errors."""
    if not path.exists():
        return
    import time
    for attempt in range(8):
        try:
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
        except Exception:
            pass
        time.sleep(0.15 * (attempt + 1))
    if path.exists():
        logger.warning(
            "Could not fully remove %s after retries (Windows lock?). Leaving in place.", path
        )


class SchemaSyncService:
    """Owns fingerprint checks, rebuilds, cache replacement, and sync history.

    Per-database knowledge is stored under knowledge/<DB_NAME>/ via
    db_context.get_knowledge_dir(). User-owned files (business_dictionary.json,
    table_annotations.json) are NEVER saved/restored across rebuilds — they
    live in the per-database directory and are preserved by design because the
    rebuild runner writes its output to the same namespaced directory.
    """

    def __init__(
        self,
        cache,
        refresh_callback: Callable[[], None],
        rebuild_runner: Callable[[Path], None],
        database_factory: Callable[[], Database] = Database,
    ):
        self.cache = cache
        self.refresh_callback = refresh_callback
        self.rebuild_runner = rebuild_runner
        self.database_factory = database_factory
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._status: Dict[str, Any] = {
            "status": "idle",
            "last_sync_at": None,
            "last_successful_build_at": None,
            "last_error": None,
            "rebuild_running": False,
            "history": [],
        }
        self._stored_fingerprint = schema_fingerprint.load(
            self._knowledge_dir / "schema_fingerprint.json"
        )
        self._load_history()

    @property
    def _knowledge_dir(self) -> Path:
        """Always resolved dynamically from the runtime database context."""
        return db_context.get_knowledge_dir()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def _history_path(self) -> Path:
        return self._knowledge_dir / "schema_sync_history.json"

    def _load_history(self) -> None:
        try:
            with self._history_path.open(encoding="utf-8") as file:
                saved = json.load(file)
            self._status.update({key: saved.get(key) for key in self._status if key in saved})
            self._status["rebuild_running"] = False
        except (OSError, json.JSONDecodeError):
            pass

    def _save_history(self) -> None:
        try:
            self._knowledge_dir.mkdir(parents=True, exist_ok=True)
            document = {
                **self._status,
                "database_name": db_context.active_database,
                "stored_fingerprint": schema_fingerprint.load(
                    self._knowledge_dir / "schema_fingerprint.json"
                ),
            }
            temporary = self._history_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
            temporary.replace(self._history_path)
        except Exception as exc:
            logger.warning("Could not save sync history: %s", exc)

    def _record(self, trigger: str, changed: Optional[bool], rebuilt: bool) -> None:
        event = {
            "at": self._now(),
            "trigger": trigger,
            "status": self._status["status"],
            "changed": changed,
            "rebuilt": rebuilt,
            "error": self._status.get("last_error"),
        }
        self._status["history"] = (self._status.get("history") or [])[-49:] + [event]

    def _current_fingerprint(self) -> str:
        database = self.database_factory()
        try:
            database.connect()
            schema = DatabaseScanner(database, db_context.active_database).scan_database()
            return schema_fingerprint.compute(schema)
        finally:
            database.close()

    def _run_rebuild(self, staging_dir: Path) -> None:
        """Run a staged rebuild; retain compatibility with older test hooks."""
        try:
            takes_no_arguments = len(inspect.signature(self.rebuild_runner).parameters) == 0
        except (TypeError, ValueError):
            takes_no_arguments = False
        if takes_no_arguments:
            self.rebuild_runner()  # pragma: no cover - compatibility boundary
        else:
            self.rebuild_runner(staging_dir)

    @staticmethod
    def _validate_staging_directory(staging_dir: Path) -> Dict[str, Any]:
        """Validate the exported runtime contract and return useful diagnostics.

        Export names changed from grouping/semantics/ranking to the more
        explicit table_groups/business_semantics/table_importance names. Keep
        legacy aliases only as an upgrade bridge; future export-name changes
        belong in ``core.knowledge_files`` so validation stays synchronized.
        """
        found = sorted(path.name for path in staging_dir.iterdir()) if staging_dir.exists() else []
        missing = []
        legacy_used = {}
        for required in REQUIRED_KNOWLEDGE_FILES:
            if (staging_dir / required).is_file():
                continue
            legacy = LEGACY_KNOWLEDGE_FILE_ALIASES.get(required)
            if legacy and (staging_dir / legacy).is_file():
                # Cache loading uses canonical names. Create a compatibility
                # copy only when the current exporter name is absent, so an
                # older exporter can still complete a safe database switch.
                shutil.copy2(staging_dir / legacy, staging_dir / required)
                legacy_used[required] = legacy
                continue
            missing.append(required)
        found = sorted(path.name for path in staging_dir.iterdir()) if staging_dir.exists() else []
        return {
            "valid": not missing,
            "staging_directory": str(staging_dir),
            "missing": missing,
            "found": found,
            "legacy_used": legacy_used,
        }

    def status(self) -> Dict[str, Any]:
        stored = schema_fingerprint.load(self._knowledge_dir / "schema_fingerprint.json")
        return {
            **self._status,
            "database_name": db_context.active_database,
            "stored_fingerprint": stored,
            "current_fingerprint": self._status.get("current_fingerprint"),
            "knowledge_version": stored[:12] if stored else None,
        }

    def synchronize(self, trigger: str = "manual") -> Dict[str, Any]:
        """Synchronize once. A concurrent caller is rejected without waiting."""
        if not self._lock.acquire(blocking=False):
            return {
                **self.status(),
                "status": "already_running",
                "message": "A schema synchronization job is already running.",
            }
        backup_dir: Optional[Path] = None
        staging_dir: Optional[Path] = None
        promoted = False
        try:
            self._status.update(
                {"status": "checking", "rebuild_running": True, "last_error": None}
            )
            database_registry.load()
            database_registry.touch_connected()
            logger.info("[Registry] Database recognized: %s", db_context.active_database)
            current = self._current_fingerprint()
            stored = schema_fingerprint.load(self._knowledge_dir / "schema_fingerprint.json")
            self._status["current_fingerprint"] = current

            artifacts = inspect_cached_artifacts(self._knowledge_dir) if current == stored else {"valid": False}
            if current == stored and artifacts["valid"]:
                database_registry.reconcile_verified_cache(
                    current, artifacts["table_count"], artifacts["profile_count"],
                    live_verified=True,
                )
                self._status.update(
                    {"status": "unchanged", "last_sync_at": self._now(), "rebuild_running": False}
                )
                self._record(trigger, changed=False, rebuilt=False)
                self._save_history()
                logger.info("[Registry] Existing knowledge loaded — schema unchanged (trigger=%s)", trigger)
                logger.info("[Profiles] Manual knowledge preserved")
                return {**self.status(), "changed": False, "rebuilt": False, "trigger": trigger}

            if current == stored:
                logger.warning("Cached knowledge is incomplete despite an unchanged live schema: %s", artifacts["issues"])

            # The production runner writes only to staging, so its active
            # directory can be moved aside after validation without a second
            # full copy. Legacy zero-argument runners may write to active;
            # retain a pre-build snapshot for that compatibility path.
            backup_dir = self._knowledge_dir.with_name(self._knowledge_dir.name + ".sync-backup")
            _safe_rmtree(backup_dir)
            try:
                legacy_runner = len(inspect.signature(self.rebuild_runner).parameters) == 0
            except (TypeError, ValueError):
                legacy_runner = False
            if legacy_runner and self._knowledge_dir.exists():
                try:
                    shutil.copytree(self._knowledge_dir, backup_dir)
                except Exception as exc:
                    _safe_rmtree(backup_dir)
                    backup_dir = None
                    raise RuntimeError("Cannot create rollback snapshot for legacy rebuild") from exc

            active_dir = self._knowledge_dir
            staging_dir = active_dir.with_name(active_dir.name + ".sync-staging")
            _safe_rmtree(staging_dir)
            if active_dir.exists():
                shutil.copytree(active_dir, staging_dir)
            else:
                staging_dir.mkdir(parents=True, exist_ok=True)

            self._status["status"] = "rebuilding"
            self._run_rebuild(staging_dir)

            # NOTE: User-owned files (business_dictionary.json, table_annotations.json)
            # are intentionally NOT restored here. They live in knowledge/<DB_NAME>/
            # and the rebuild runner writes to that same namespaced directory, so they
            # are preserved by design. The old _save_user_files/_restore_user_files
            # pattern was the root cause of cross-database state pollution and has been removed.

            diagnostics = self._validate_staging_directory(staging_dir)
            if not diagnostics["valid"]:
                raise RuntimeError(
                    "Staged knowledge build is incomplete.\n"
                    f"Staging directory: {diagnostics['staging_directory']}\n"
                    f"Missing: {', '.join(diagnostics['missing']) or '(none)'}\n"
                    f"Found: {', '.join(diagnostics['found']) or '(none)'}"
                )
            if diagnostics["legacy_used"]:
                logger.warning(
                    "Staged knowledge build uses legacy filenames: %s. "
                    "Upgrade the exporter to the canonical names.", diagnostics["legacy_used"]
                )
            schema_fingerprint.save(current, staging_dir / "schema_fingerprint.json")
            structural = inspect_cached_artifacts(staging_dir)
            if not structural["valid"] or structural["fingerprint"] != current:
                raise RuntimeError(
                    "Staged knowledge does not match the live schema or lacks table profiles: "
                    + "; ".join(structural["issues"] or ["fingerprint mismatch"])
                )

            # Promote only a validated build. The old directory is retained as
            # rollback material until cache activation succeeds.
            active_dir = self._knowledge_dir
            if active_dir.exists():
                _safe_rmtree(backup_dir)
                active_dir.replace(backup_dir)
            staging_dir.replace(active_dir)
            staging_dir = None
            promoted = True
            self.cache.load()
            self.refresh_callback()
            database_registry.load()
            database_registry.reconcile_verified_cache(
                current, structural["table_count"], structural["profile_count"],
                live_verified=True,
            )
            self._stored_fingerprint = current
            self._status.update(
                {
                    "status": "synchronized",
                    "last_sync_at": self._now(),
                    "last_successful_build_at": self._now(),
                    "rebuild_running": False,
                }
            )
            self._record(trigger, changed=True, rebuilt=True)
            self._save_history()
            logger.info("Schema sync completed (trigger=%s)", trigger)

            if backup_dir:
                try:
                    _safe_rmtree(backup_dir)
                except Exception as exc:
                    logger.warning("Could not remove sync backup directory: %s", exc)

            return {**self.status(), "changed": True, "rebuilt": True, "trigger": trigger}

        except (Exception, SystemExit) as exc:
            logger.exception("Schema synchronization failed")
            if staging_dir:
                _safe_rmtree(staging_dir)
            if backup_dir and backup_dir.exists():
                try:
                    if self._knowledge_dir.exists():
                        _safe_rmtree(self._knowledge_dir)
                    shutil.move(str(backup_dir), str(self._knowledge_dir))
                    logger.info("Rolled back knowledge directory from backup")
                    if promoted:
                        # A failed cache activation may have partially changed
                        # in-memory state. Reload the known-good runtime too.
                        self.cache.load()
                        self.refresh_callback()
                except Exception as roll_exc:
                    logger.error(
                        "Rollback failed: %s — knowledge directory may be inconsistent", roll_exc
                    )

            self._status.update(
                {
                    "status": "failed",
                    "last_error": str(exc),
                    "last_sync_at": self._now(),
                    "rebuild_running": False,
                }
            )
            self._record(trigger, changed=None, rebuilt=False)
            self._save_history()
            return {
                **self.status(),
                "changed": None,
                "rebuilt": False,
                "trigger": trigger,
                "error": str(exc),
            }
        finally:
            self._status["rebuild_running"] = False
            self._lock.release()

    def start_background_worker(self, interval_seconds: int) -> None:
        if self._worker and self._worker.is_alive():
            return

        def worker() -> None:
            """Background sync loop. Exceptions are fully contained — the
            worker must NEVER let an unhandled exception propagate to the
            threading layer, which would cause Flask's debug watchdog to
            detect the crash and restart the entire process."""
            while not self._stop_event.wait(interval_seconds):
                try:
                    self.synchronize(trigger="background")
                except Exception as exc:
                    logger.error(
                        "Unexpected error in schema sync worker: %s", exc, exc_info=True
                    )

        self._worker = threading.Thread(target=worker, name="schema-sync", daemon=True)
        self._worker.start()
        logger.info(
            "Schema sync background worker started (interval=%ss)", interval_seconds
        )

    def stop_background_worker(self, timeout: float = 5.0) -> None:
        """Signal the background worker to stop and wait for termination."""
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=timeout)
            if self._worker.is_alive():
                logger.warning(
                    "Schema sync background worker did not terminate within %s seconds", timeout
                )
            else:
                logger.info("Schema sync background worker stopped successfully")
            self._worker = None
