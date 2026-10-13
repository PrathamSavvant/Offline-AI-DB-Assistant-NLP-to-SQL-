import json

from core.knowledge_files import REQUIRED_KNOWLEDGE_FILES
from offline_bootstrap import invalid_knowledge_files, cache_issues
from core import schema_fingerprint


def test_offline_bootstrap_detects_missing_empty_and_corrupt_files(tmp_path):
    for name in REQUIRED_KNOWLEDGE_FILES:
        (tmp_path / name).write_text(json.dumps({"ok": True}), encoding="utf-8")
    assert invalid_knowledge_files(tmp_path) == []

    missing = REQUIRED_KNOWLEDGE_FILES[0]
    (tmp_path / missing).unlink()
    assert missing in invalid_knowledge_files(tmp_path)

    (tmp_path / missing).write_text("", encoding="utf-8")
    assert missing in invalid_knowledge_files(tmp_path)

    (tmp_path / missing).write_text("not-json", encoding="utf-8")
    assert missing in invalid_knowledge_files(tmp_path)


def test_bootstrap_rejects_missing_profiles_even_when_json_files_exist(tmp_path):
    schema = {"tables": [{"name": "opaque", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}]}
    for name in REQUIRED_KNOWLEDGE_FILES:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
    schema_fingerprint.save(schema_fingerprint.compute(schema), tmp_path / "schema_fingerprint.json")
    assert invalid_knowledge_files(tmp_path) == []
    assert any("profile" in issue for issue in cache_issues(tmp_path))
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "opaque.json").write_text("{}", encoding="utf-8")
    assert cache_issues(tmp_path) == []
