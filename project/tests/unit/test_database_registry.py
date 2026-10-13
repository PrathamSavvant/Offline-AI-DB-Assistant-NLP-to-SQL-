import json
from pathlib import Path
from core.database_registry import DatabaseRegistry


def test_registry_creates_default_file_on_first_load(tmp_path):
    reg_file = tmp_path / "database.json"
    registry = DatabaseRegistry(path=reg_file)

    data = registry.load()
    assert data["database_id"] is not None
    assert data["knowledge_status"] == "never_scanned"
    assert data["knowledge_version"] == "1"
    assert reg_file.exists()


def test_registry_update_after_scan_persists_changes(tmp_path):
    reg_file = tmp_path / "database.json"
    registry = DatabaseRegistry(path=reg_file)
    registry.load()

    registry.update_after_scan(
        fingerprint="sha256_fake_fingerprint",
        table_count=12,
        profile_count=12,
        status="up_to_date",
    )

    data = registry.get()
    assert data["fingerprint"] == "sha256_fake_fingerprint"
    assert data["table_count"] == 12
    assert data["profile_count"] == 12
    assert data["knowledge_status"] == "up_to_date"
    assert data["knowledge_version"] == "2"
    assert data["last_scan"] is not None


def test_registry_touch_connected_updates_timestamp(tmp_path):
    reg_file = tmp_path / "database.json"
    registry = DatabaseRegistry(path=reg_file)
    registry.load()

    prev_connected = registry.get().get("last_connected")
    registry.touch_connected()
    new_connected = registry.get().get("last_connected")
    assert new_connected is not None


def test_registry_identity_is_stable_across_reconnect_and_schema_changes(tmp_path):
    reg_file = tmp_path / "database.json"
    first = DatabaseRegistry(path=reg_file)
    first_data = first.load()
    first.update_after_scan("schema-with-204-tables", 204, 204, "up_to_date")

    # A new registry instance represents an application restart.  A changed
    # fingerprint is schema state, not database identity.
    restarted = DatabaseRegistry(path=reg_file)
    assert restarted.load()["database_id"] == first_data["database_id"]
    restarted.update_after_scan("schema-with-205-tables", 205, 205, "up_to_date")
    assert restarted.get()["database_id"] == first_data["database_id"]
