"""
tests/unit/test_schema_sync_service.py

Tests SchemaSyncService fingerprint logic without touching a real database.
The service resolves its knowledge directory from db_context, so tests must
write seed data into that same directory.
"""
from pathlib import Path
import json

import pytest

from core import schema_fingerprint
from core.db_context import db_context
from core.knowledge_files import REQUIRED_KNOWLEDGE_FILES
from core.database_registry import database_registry
from services.schema_sync_service import SchemaSyncService


class Cache:
    def __init__(self): self.loads = 0
    def load(self): self.loads += 1


class Sync(SchemaSyncService):
    """Subclass that overrides _current_fingerprint so no DB connection is needed."""

    def __init__(self, *args, fingerprint="new", **kwargs):
        super().__init__(*args, **kwargs)
        self.fingerprint = fingerprint

    def _current_fingerprint(self):
        return self.fingerprint


def _kdir() -> Path:
    """Convenience: return the path that the service will use."""
    return db_context.get_knowledge_dir()


@pytest.fixture(autouse=True)
def isolated_knowledge_dir(tmp_path_factory, monkeypatch):
    """Sync tests must never mutate any configured user's knowledge files."""
    directory = tmp_path_factory.mktemp("sync-runtime")
    monkeypatch.setattr(db_context, "get_knowledge_dir", lambda: directory)
    monkeypatch.setattr(database_registry, "_custom_path", directory / "database.json")
    database_registry.load()
    yield directory


def _seed_complete_cache(directory):
    schema = {"tables": [{"name": "example", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]}
    for name in REQUIRED_KNOWLEDGE_FILES:
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (directory / "tables").mkdir()
    (directory / "tables" / "example.json").write_text("{}", encoding="utf-8")
    fingerprint = schema_fingerprint.compute(schema)
    schema_fingerprint.save(fingerprint, directory / "schema_fingerprint.json")
    return fingerprint


def _write_complete_staged_cache(directory):
    schema = {"tables": [{"name": "new_table", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]}
    for name in REQUIRED_KNOWLEDGE_FILES:
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (directory / "tables").mkdir(exist_ok=True)
    (directory / "tables" / "new_table.json").write_text("{}", encoding="utf-8")
    return schema_fingerprint.compute(schema)


def test_unchanged_schema_skips_rebuild():
    kdir = _kdir()
    kdir.mkdir(parents=True, exist_ok=True)
    fingerprint = _seed_complete_cache(kdir)

    rebuilt, refreshed = [], []
    cache = Cache()
    service = Sync(cache, lambda: refreshed.append(True), lambda: rebuilt.append(True), fingerprint=fingerprint)
    report = service.synchronize()

    assert report["rebuilt"] is False
    assert not rebuilt
    assert cache.loads == 0
    assert not refreshed


def test_changed_schema_rebuilds_and_refreshes_cache():
    kdir = _kdir()
    kdir.mkdir(parents=True, exist_ok=True)
    schema_fingerprint.save("old", kdir / "schema_fingerprint.json")

    cache, rebuilt, refreshed = Cache(), [], []
    new_schema = {"tables": [{"name": "new_table", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]}
    new_fingerprint = schema_fingerprint.compute(new_schema)
    def rebuild(staging):
        rebuilt.append(True)
        assert _write_complete_staged_cache(staging) == new_fingerprint
    service = Sync(cache, lambda: refreshed.append(True), rebuild, fingerprint=new_fingerprint)
    report = service.synchronize()

    assert report["rebuilt"] is True
    assert rebuilt
    assert refreshed
    assert cache.loads == 1
    assert schema_fingerprint.load(kdir / "schema_fingerprint.json") == new_fingerprint


def test_failed_rebuild_preserves_previous_fingerprint():
    kdir = _kdir()
    kdir.mkdir(parents=True, exist_ok=True)
    schema_fingerprint.save("old", kdir / "schema_fingerprint.json")

    marker = kdir / "marker.txt"
    marker.write_text("old", encoding="utf-8")

    def fail():
        marker.write_text("new", encoding="utf-8")
        raise RuntimeError("builder failed")

    service = Sync(Cache(), lambda: None, fail, fingerprint="new")
    report = service.synchronize()

    assert report["status"] == "failed"
    assert schema_fingerprint.load(kdir / "schema_fingerprint.json") == "old"
    # The rollback restored marker.txt to its pre-failure state
    assert marker.read_text(encoding="utf-8") == "old"


def test_failed_cache_activation_restores_previous_runtime():
    directory = _kdir()
    directory.mkdir(parents=True, exist_ok=True)
    schema_fingerprint.save("old", directory / "schema_fingerprint.json")
    (directory / "marker.txt").write_text("old", encoding="utf-8")

    class FailOnceCache:
        loads = 0
        def load(self):
            self.loads += 1
            if self.loads == 1:
                raise RuntimeError("activation failed")

    def rebuild(staging):
        (staging / "marker.txt").write_text("new", encoding="utf-8")
        _write_complete_staged_cache(staging)

    refreshed = []
    cache = FailOnceCache()
    new_fingerprint = schema_fingerprint.compute({"tables": [{"name": "new_table", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]})
    report = Sync(cache, lambda: refreshed.append(True), rebuild, fingerprint=new_fingerprint).synchronize()
    assert report["status"] == "failed"
    assert (directory / "marker.txt").read_text(encoding="utf-8") == "old"
    assert schema_fingerprint.load(directory / "schema_fingerprint.json") == "old"
    assert cache.loads == 2
    assert refreshed == [True]


def test_incomplete_staged_profiles_never_replace_active_cache():
    directory = _kdir()
    directory.mkdir(parents=True, exist_ok=True)
    old_fingerprint = _seed_complete_cache(directory)
    new_schema = {"tables": [{"name": "new_table", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]}
    new_fingerprint = schema_fingerprint.compute(new_schema)
    def incomplete(staging):
        for name in REQUIRED_KNOWLEDGE_FILES:
            (staging / name).write_text("{}", encoding="utf-8")
        (staging / "schema.json").write_text(json.dumps(new_schema), encoding="utf-8")
        # Deliberately omit the new table profile.
    report = Sync(Cache(), lambda: None, incomplete, fingerprint=new_fingerprint).synchronize()
    assert report["status"] == "failed"
    assert schema_fingerprint.load(directory / "schema_fingerprint.json") == old_fingerprint
    assert (directory / "tables" / "example.json").exists()


def test_staging_validation_reports_missing_and_found_files(tmp_path):
    (tmp_path / "schema.json").write_text("{}", encoding="utf-8")
    diagnostics = SchemaSyncService._validate_staging_directory(tmp_path)

    assert diagnostics["valid"] is False
    assert "relationships.json" in diagnostics["missing"]
    assert diagnostics["found"] == ["schema.json"]


def test_staging_validation_accepts_legacy_names_when_canonical_is_absent(tmp_path):
    for name in REQUIRED_KNOWLEDGE_FILES:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    for canonical, legacy in {
        "table_groups.json": "grouping.json",
        "business_semantics.json": "semantics.json",
        "table_importance.json": "ranking.json",
    }.items():
        (tmp_path / canonical).unlink()
        (tmp_path / legacy).write_text("{}", encoding="utf-8")

    diagnostics = SchemaSyncService._validate_staging_directory(tmp_path)
    assert diagnostics["valid"] is True
    assert diagnostics["legacy_used"]["table_groups.json"] == "grouping.json"
