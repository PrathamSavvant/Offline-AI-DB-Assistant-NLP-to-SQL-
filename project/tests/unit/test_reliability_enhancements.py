import json

import pytest

from core import schema_fingerprint
from core.database_registry import DatabaseRegistry
from core.knowledge_files import REQUIRED_KNOWLEDGE_FILES
from core.knowledge_readiness import inspect_cached_artifacts
from core.query_cost import QueryCostError, check_explain_plan
from core.query_evaluation import run_suite, evaluate_full_case
from import_knowledge import apply_document, validate_document
from services.sql_reasoning_engine import SQLReasoningEngine


SCHEMA = {"tables": [
    {"name": "opaque_a", "columns": [{"name": "id", "datatype": "int"}, {"name": "state", "datatype": "varchar"}], "foreign_keys": []},
    {"name": "opaque_b", "columns": [{"name": "id", "datatype": "int"}, {"name": "a_id", "datatype": "int"}], "foreign_keys": [
        {"column": "a_id", "references_table": "opaque_a", "references_column": "id"}]},
]}


def _artifact_dir(path):
    path.mkdir()
    for name in REQUIRED_KNOWLEDGE_FILES:
        (path / name).write_text("{}", encoding="utf-8")
    (path / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    schema_fingerprint.save(schema_fingerprint.compute(SCHEMA), path / "schema_fingerprint.json")
    (path / "tables").mkdir()
    for name in ("opaque_a", "opaque_b"):
        (path / "tables" / f"{name}.json").write_text("{}", encoding="utf-8")
    return path


def test_cached_artifact_audit_detects_missing_profile_and_fingerprint(tmp_path):
    path = _artifact_dir(tmp_path / "knowledge")
    assert inspect_cached_artifacts(path)["valid"]
    (path / "tables" / "opaque_b.json").unlink()
    assert not inspect_cached_artifacts(path)["valid"]
    (path / "tables" / "opaque_b.json").write_text("{}", encoding="utf-8")
    schema_fingerprint.save("wrong", path / "schema_fingerprint.json")
    assert "Cached schema does not match its fingerprint" in inspect_cached_artifacts(path)["issues"]


def test_registry_reconciliation_does_not_increment_knowledge_version(tmp_path):
    registry = DatabaseRegistry(tmp_path / "database.json")
    registry.load()
    registry.reconcile_verified_cache("fingerprint", 2, 2)
    assert registry.get()["knowledge_status"] == "up_to_date"
    assert registry.get()["table_count"] == 2
    assert registry.get()["knowledge_version"] == "1"
    registry.reconcile_verified_cache("fingerprint", 2, 2, live_verified=True)
    assert registry.get()["last_scan"] is not None
    with pytest.raises(ValueError):
        registry.reconcile_verified_cache("fingerprint", 2, 1)


def test_gold_suite_reports_missing_tables_and_predicates_without_values():
    class Builder:
        def build(self, _question):
            return {
                "selected_tables": ["opaque_a"],
                "scored_tables": [{"table": "opaque_a"}],
                "query_plan": {"status": "ready", "resolved_predicates": []},
                "join_plan": {"included_tables": ["opaque_a"], "all_joins_verified": True},
            }
    report = run_suite(Builder(), [{"id": "case-1", "question": "private value",
                                    "expected_tables": ["opaque_a", "opaque_b"],
                                    "expected_predicates": [{"table": "opaque_a", "column": "state", "value": "SECRET"}]}])
    assert report["failed"] == 1
    assert report["cases"][0]["missing_tables"] == ["opaque_b"]
    assert report["cases"][0]["missing_predicates"] == [{"table": "opaque_a", "column": "state"}]
    assert "SECRET" not in json.dumps(report)
    assert "private value" not in json.dumps(report)


def test_knowledge_import_validates_identity_and_preserves_existing_entries(tmp_path):
    fingerprint = schema_fingerprint.compute(SCHEMA)
    document = {"database_id": "db-1", "schema_fingerprint": fingerprint, "tables": {
        "opaque_a": {"definition": "Equipment record", "columns": {"state": {"business_meaning": "current status"}},
                     "predicate_mappings": [{"phrase": "ready", "column": "state", "value": "R"}]}
    }}
    assert validate_document(document, SCHEMA, "db-1", fingerprint) == []
    target = tmp_path / "defined_knowledge.json"
    apply_document(document, target)
    from core.defined_knowledge import DefinedKnowledgeRepository
    assert DefinedKnowledgeRepository(target).table("opaque_a")["definition"] == "Equipment record"
    bad = {**document, "database_id": "another-db"}
    assert validate_document(bad, SCHEMA, "db-1", fingerprint)
    assert DefinedKnowledgeRepository(target).table("opaque_a")["definition"] == "Equipment record"


def test_knowledge_import_rejects_new_predicate_conflict_atomically(tmp_path):
    from core.defined_knowledge import DefinedKnowledgeRepository
    target = tmp_path / "defined_knowledge.json"
    repository = DefinedKnowledgeRepository(target)
    repository.upsert_predicate("opaque_a", {"phrase": "ready", "column": "state", "value": "R"})
    before = target.read_bytes()
    document = {"tables": {"opaque_b": {"predicate_mappings": [
        {"phrase": "ready", "column": "a_id", "value": 1}
    ]}}}
    with pytest.raises(ValueError, match="conflicting predicate"):
        apply_document(document, target)
    assert target.read_bytes() == before


def test_explain_gate_rejects_large_scan_and_join():
    with pytest.raises(QueryCostError, match="table scan"):
        check_explain_plan([{"rows": 3000000}], 2000000, 5000000)
    with pytest.raises(QueryCostError, match="join work"):
        check_explain_plan([{"rows": 3000}, {"rows": 3000}], 2000000, 5000000)
    assert check_explain_plan([{"rows": 100}, {"rows": 4}], 2000000, 5000000)["estimated_join_rows"] == 400


def test_executor_checks_explain_before_running_sql(monkeypatch):
    from config.settings import settings
    from services.sql_reasoning_engine import DatabaseExecutor
    monkeypatch.setattr(settings, "DB_MAX_ESTIMATED_SCAN_ROWS", 10)
    class Adapter:
        executed = False
        closed = False
        def explain(self, _sql): return [{"rows": 100}]
        def execute(self, _sql):
            self.executed = True
            return []
        def close(self): self.closed = True
    adapter = Adapter()
    with pytest.raises(QueryCostError):
        DatabaseExecutor(adapter).execute("SELECT 1")
    assert not adapter.executed
    assert adapter.closed


def test_full_evaluation_reports_no_rows_or_sql():
    class Engine:
        last_prompt_info = {"llama_response": {"confidence": 0.7}}
        def execute(self, _question):
            return {"sql": "SELECT 'SECRET'", "rows": [{"value": "SECRET"}],
                    "row_count": 1, "columns": ["value"]}
    report = evaluate_full_case(Engine(), {"id": "case-1", "question": "SECRET",
                                                 "expected_row_count": 1, "expected_columns": ["value"]})
    assert report["passed"]
    assert "SECRET" not in json.dumps(report)


def test_model_clarification_cannot_execute_non_null_sql():
    class Builder:
        def build(self, _question):
            return {"query_plan": {"status": "ready"}, "selected_tables": ["opaque_a"]}
    class Llama:
        def compose(self, _context):
            return "prompt", {"sql": "SELECT * FROM opaque_a", "answer": "Which state?", "needs_clarification": True}
    class NoExecute:
        def __call__(self):
            raise AssertionError("Database must not be called")
    engine = SQLReasoningEngine(Builder(), None, None, NoExecute(), None, llama_agent=Llama())
    result = engine.execute("show equipment")
    assert result["sql"] is None
    assert result["sql_executed"] is False


def test_grounded_single_table_count_skips_llama_generation():
    class Builder:
        def build(self, _question):
            return {
                "selected_tables": ["employees"],
                "retrieved_tables": ["employees"],
                "query_plan": {
                    "status": "ready",
                    "primary_entity": "employees",
                    "selected_tables": ["employees"],
                    "requested_tables": ["employees"],
                    "result_type": "COUNT",
                    "operation": {"type": "COUNT"},
                    "resolved_predicates": [],
                    "verified_join_path": [],
                    "approved_join_path": [],
                    "ambiguities": [],
                },
            }

    class Validator:
        def validate(self, sql, _context):
            assert sql == "SELECT COUNT(*) AS count FROM `employees`"
            return sql

    class Adapter:
        def explain(self, _sql): return [{"rows": 300024, "filtered": 100}]
        def execute(self, _sql): return [{"count": 300024}]
        def close(self): pass

    class Llama:
        def compose(self, _context):
            raise AssertionError("A grounded single-table count must not call Llama")

    diagnostics = {}
    engine = SQLReasoningEngine(
        Builder(), None, Validator(), Adapter, None,
        last_prompt_info=diagnostics, llama_agent=Llama(),
    )
    result = engine.execute("how many employees are there")

    assert result["rows"] == [{"count": 300024}]
    assert result["llama_sql_called"] is False
    assert result["sql_executed"] is True
    assert diagnostics["llama_sql_called"] is False


def test_query_api_fails_closed_during_failed_schema_sync(monkeypatch):
    from app import app
    services = app.extensions["services"]
    monkeypatch.setattr(services["schema_sync"], "status", lambda: {"status": "failed", "last_sync_at": None})
    response = app.test_client().post("/api/execute", json={"sql": "SELECT 1"})
    assert response.status_code == 503
    assert response.get_json()["knowledge_readiness"]["schema_ready"] is False


def test_sensitive_debug_api_is_opt_in(monkeypatch):
    from app import app
    from config.settings import settings
    monkeypatch.setattr(settings, "ENABLE_SENSITIVE_DEBUG", False)
    client = app.test_client()
    assert client.get("/api/debug/prompt").status_code == 403
    assert client.get("/api/logs").status_code == 403
