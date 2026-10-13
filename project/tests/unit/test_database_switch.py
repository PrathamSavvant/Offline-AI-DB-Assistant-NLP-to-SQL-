"""
tests/unit/test_database_switch.py

Validates that switching databases via db_context propagates correctly:
  - db_context.active_database updates
  - settings.DB_* synchronises
  - get_knowledge_dir() resolves to the namespaced path
  - Store .path properties resolve dynamically (no cached stale path)
  - Listeners fire in order
  - memory_store.clear_all() purges all thread state
"""

import threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_context(db_context, dbname: str):
    """Force the runtime context to a known starting state without notifying listeners."""
    db_context._active_database = dbname


# ---------------------------------------------------------------------------
# core/db_context tests
# ---------------------------------------------------------------------------

class TestRuntimeDatabaseContext:

    def test_initial_active_database_matches_settings(self):
        from core.db_context import db_context
        from config.settings import settings
        assert db_context.active_database == settings.DB_NAME

    def test_set_active_database_updates_property(self):
        from core.db_context import db_context
        _reset_context(db_context, "CompanyDB")
        db_context.set_active_database("UniversityDB")
        assert db_context.active_database == "UniversityDB"

    def test_set_active_database_syncs_settings(self):
        from core.db_context import db_context
        from config.settings import settings
        db_context.set_active_database("UniversityDB", host="localhost", port=3307, user="root", password="pw")
        assert settings.DB_NAME == "UniversityDB"
        assert settings.DB_HOST == "localhost"
        assert settings.DB_PORT == 3307

    def test_set_active_database_empty_raises(self):
        from core.db_context import db_context
        with pytest.raises(ValueError, match="cannot be empty"):
            db_context.set_active_database("")

    def test_get_knowledge_dir_namespaced(self):
        from core.db_context import db_context
        db_context.set_active_database("CompanyDB")
        kdir = db_context.get_knowledge_dir()
        assert kdir.name.startswith("CompanyDB--")
        assert kdir.parent.name == "knowledge"

    def test_get_knowledge_dir_changes_on_switch(self):
        from core.db_context import db_context
        db_context.set_active_database("CompanyDB")
        dir_a = db_context.get_knowledge_dir()
        db_context.set_active_database("UniversityDB")
        dir_b = db_context.get_knowledge_dir()
        assert dir_a != dir_b
        assert dir_b.name.startswith("UniversityDB--")

    def test_listeners_called_on_switch(self):
        from core.db_context import RuntimeDatabaseContext
        ctx = RuntimeDatabaseContext()
        calls = []
        ctx.register_listener(lambda db: calls.append(db))
        ctx.set_active_database("UniversityDB")
        assert calls == ["UniversityDB"]

    def test_multiple_listeners_called_in_order(self):
        from core.db_context import RuntimeDatabaseContext
        ctx = RuntimeDatabaseContext()
        order = []
        ctx.register_listener(lambda db: order.append(f"first:{db}"))
        ctx.register_listener(lambda db: order.append(f"second:{db}"))
        ctx.set_active_database("TestDB")
        assert order == ["first:TestDB", "second:TestDB"]

    def test_listener_error_does_not_propagate(self):
        from core.db_context import RuntimeDatabaseContext
        ctx = RuntimeDatabaseContext()
        ctx.register_listener(lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
        # Should not raise
        ctx.set_active_database("SafeDB")
        assert ctx.active_database == "SafeDB"

    def test_get_knowledge_dir_sanitises_special_chars(self):
        from core.db_context import RuntimeDatabaseContext
        ctx = RuntimeDatabaseContext()
        ctx._active_database = "My Database!"
        kdir = ctx.get_knowledge_dir()
        # special chars replaced with underscores
        assert "!" not in kdir.name
        assert " " not in kdir.name

    def test_round_trip_switch(self):
        """CompanyDB -> UniversityDB -> CompanyDB produces correct paths each time."""
        from core.db_context import db_context
        for db in ["CompanyDB", "UniversityDB", "CompanyDB", "UniversityDB"]:
            db_context.set_active_database(db)
            assert db_context.active_database == db
            assert db_context.get_knowledge_dir().name.startswith(f"{db}--")


# ---------------------------------------------------------------------------
# Store path property tests (dynamic resolution)
# ---------------------------------------------------------------------------

class TestStoreDynamicPaths:

    def test_business_dictionary_path_changes_on_switch(self, tmp_path):
        from core.db_context import db_context
        from core.business_dictionary import BusinessDictionaryStore
        db_context.set_active_database("CompanyDB")
        store = BusinessDictionaryStore(seed_if_empty=False)
        path_a = store.path
        db_context.set_active_database("UniversityDB")
        path_b = store.path
        assert "CompanyDB" in str(path_a)
        assert "UniversityDB" in str(path_b)
        assert path_a != path_b

    def test_table_annotations_path_changes_on_switch(self):
        from core.db_context import db_context
        from core.table_annotations import TableAnnotationStore
        db_context.set_active_database("CompanyDB")
        store = TableAnnotationStore()
        path_a = store.path
        db_context.set_active_database("UniversityDB")
        path_b = store.path
        assert "CompanyDB" in str(path_a)
        assert "UniversityDB" in str(path_b)

    def test_pending_dictionary_path_changes_on_switch(self):
        from core.db_context import db_context
        from core.pending_dictionary import PendingDictionaryStore
        db_context.set_active_database("CompanyDB")
        store = PendingDictionaryStore()
        path_a = store.path
        db_context.set_active_database("UniversityDB")
        path_b = store.path
        assert "CompanyDB" in str(path_a)
        assert "UniversityDB" in str(path_b)

    def test_schema_cache_knowledge_dir_changes_on_switch(self):
        from core.db_context import db_context
        from core.schema_cache import SchemaCache
        cache = SchemaCache()
        db_context.set_active_database("CompanyDB")
        dir_a = cache.knowledge_dir
        db_context.set_active_database("UniversityDB")
        dir_b = cache.knowledge_dir
        assert "CompanyDB" in str(dir_a)
        assert "UniversityDB" in str(dir_b)


# ---------------------------------------------------------------------------
# memory_store.clear_all tests
# ---------------------------------------------------------------------------

class TestMemoryStoreClearAll:

    def test_clear_all_removes_all_threads(self):
        from agent.memory import MemoryStore
        store = MemoryStore()
        store.add_message("thread-1", "user", "hello")
        store.add_message("thread-2", "assistant", "world")
        store.update_context("thread-1", last_sql="SELECT 1")
        store.clear_all()
        assert store.get_history("thread-1") == []
        assert store.get_history("thread-2") == []
        assert store.get_context("thread-1") == {}

    def test_clear_all_thread_safe(self):
        from agent.memory import MemoryStore
        store = MemoryStore()
        for i in range(50):
            store.add_message(f"thread-{i}", "user", f"msg-{i}")
        errors = []

        def writer():
            for i in range(100):
                try:
                    store.add_message("concurrent", "user", f"msg-{i}")
                except Exception as exc:
                    errors.append(exc)

        def clearer():
            for _ in range(5):
                store.clear_all()

        threads = [threading.Thread(target=writer) for _ in range(4)]
        threads.append(threading.Thread(target=clearer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Thread safety errors: {errors}"
