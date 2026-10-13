import json

from core.knowledge_search import KnowledgeSearchEngine
from core.table_knowledge import TableKnowledgeBuilder, TableKnowledgeRepository


SCHEMA = {
    "tables": [
        {
            "name": "work_orders",
            "type": "BASE TABLE",
            "approx_row_count": 42,
            "primary_key": ["work_order_id"],
            "columns": [
                {"name": "work_order_id", "is_primary_key": True, "datatype": "int"},
                {"name": "equipment_name", "datatype": "varchar"},
                {"name": "repair_status", "datatype": "varchar"},
            ],
            "foreign_keys": [],
        }
    ]
}


def test_profile_contains_persistent_business_and_schema_knowledge():
    profiles = TableKnowledgeBuilder(
        SCHEMA,
        {"tables": {"work_orders": {"summary": "Stores maintenance work orders.", "business_tags": ["repair"], "column_semantics": {"work_order_id": ["identifier"], "equipment_name": ["descriptive"], "repair_status": ["status"]}}}},
        {"relationships": []},
        {"table_to_domain": {"work_orders": "Maintenance"}},
    ).build_all()
    profile = profiles["work_orders"]
    assert profile["table"] == "work_orders"
    assert profile["identity_columns"] == ["work_order_id"]
    assert profile["display_columns"] == ["equipment_name"]
    assert profile["planner_hints"]["domain"] == "Maintenance"
    assert profile["schema"]["columns"][0]["name"] == "work_order_id"


def test_profiles_serialize_and_load(tmp_path):
    repository = TableKnowledgeRepository(tmp_path / "tables")
    profiles = TableKnowledgeBuilder(SCHEMA).build_all()
    repository.save_all(profiles)
    loaded = repository.load_all()["work_orders"]
    assert loaded["profile_version"] == 2
    assert "manual_knowledge" in loaded
    assert "system_knowledge" in loaded


def test_v1_profile_migrates_to_v2(tmp_path):
    tables_dir = tmp_path / "tables"
    tables_dir.mkdir()
    v1_profile = {
        "profile_version": 1,
        "table": "work_orders",
        "purpose": "Stores work order records.",
        "abbreviations": {"WO": {"abbreviation": "WO", "expansion": "Work Order", "column": "work_order_id"}},
        "schema": SCHEMA["tables"][0],
    }
    (tables_dir / "work_orders.json").write_text(json.dumps(v1_profile), encoding="utf-8")

    repository = TableKnowledgeRepository(tables_dir)
    loaded = repository.load_all()["work_orders"]
    assert loaded["profile_version"] == 2
    assert loaded["manual_knowledge"]["definition"] == "Stores work order records."
    assert loaded["manual_knowledge"]["abbreviations"]["WO"]["meaning"] == "Work Order"
    assert loaded["manual_knowledge"]["abbreviations"]["WO"]["status"] == "ok"


def test_column_removal_marks_abbreviation_as_needs_review():
    builder = TableKnowledgeBuilder(SCHEMA)
    existing_profile = {
        "profile_version": 2,
        "table": "work_orders",
        "table_fingerprint": "old_fp",
        "manual_knowledge": {
            "definition": "Stores work orders",
            "synonyms": ["jobs"],
            "abbreviations": {
                "WO": {"abbreviation": "WO", "meaning": "Work Order", "column": "deleted_col", "status": "ok"}
            },
        },
        "system_knowledge": {},
    }
    profile = builder.build_profile(SCHEMA["tables"][0], existing=existing_profile)
    # 'deleted_col' is not in SCHEMA columns, so status should become 'needs_review'
    wo_entry = profile["manual_knowledge"]["abbreviations"]["WO"]
    assert wo_entry["status"] == "needs_review"
    assert "issue" in wo_entry
    # Manual knowledge preserved
    assert profile["manual_knowledge"]["definition"] == "Stores work orders"
    assert profile["manual_knowledge"]["synonyms"] == ["jobs"]


def test_knowledge_search_uses_business_profile_fields_not_raw_schema():
    profiles = TableKnowledgeBuilder(
        SCHEMA, annotations={"work_orders": {"meaning": "Equipment repair jobs", "aliases": ["repair jobs"]}}
    ).build_all()
    results = KnowledgeSearchEngine(profiles).search("show repair jobs")
    assert results[0]["table"] == "work_orders"
    assert any("aliases" in match or "purpose" in match for match in results[0]["matches"])


def test_legacy_raw_table_file_is_not_loaded_as_profile(tmp_path):
    tables = tmp_path / "tables"
    tables.mkdir()
    (tables / "work_orders.json").write_text(json.dumps(SCHEMA["tables"][0]), encoding="utf-8")
    assert TableKnowledgeRepository(tables).load_all() == {}
