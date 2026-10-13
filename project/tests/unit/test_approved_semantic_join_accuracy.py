"""Approved non-FK joins are usable, but never guessed or silently weakened."""
import pytest

from core.context_generator import ContextGenerator
from core.join_path_finder import JoinPathFinder
from core.query_plan import QueryPlanBuilder
from core.relationship_builder import RelationshipBuilder
from core.semantic_relationships import SemanticRelationshipRepository
from services.sql_reasoning_engine import QueryContextBuilder, SQLValidationError, SQLValidator


SCHEMA = {"database": "workshop", "tables": [
    {"name": "x_work", "columns": [
        {"name": "id", "datatype": "int"}, {"name": "group_code", "datatype": "int"},
        {"name": "shop_code", "datatype": "int"}], "foreign_keys": []},
    {"name": "x_group", "columns": [
        {"name": "id", "datatype": "int"}, {"name": "shop_id", "datatype": "int"}], "foreign_keys": []},
]}


def _edge(repository, **overrides):
    payload = {"from_table": "x_work", "from_column": "group_code",
               "to_table": "x_group", "to_column": "id",
               "description": "DBA reviewed work-to-group business key", "status": "pending"}
    payload.update(overrides)
    return repository.create(payload, schema=SCHEMA)


def _plan(repository):
    builder = RelationshipBuilder(SCHEMA, repository.approved())
    return JoinPathFinder(builder).find_multi_table_join(
        ["x_work", "x_group"], allow_semantic_fallback=True,
        fail_on_ambiguous_paths=True,
    )


def _context(plan):
    return {"selected_tables": ["x_work", "x_group"],
            "tables_by_name": {table["name"]: table for table in SCHEMA["tables"]},
            "join_plan": plan}


def test_pending_edge_cannot_join_but_approved_edge_reaches_sql_validator(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    pending = _edge(repository)
    assert _plan(repository)["unreachable_tables"] == ["x_group"]
    repository.update(pending["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    plan = _plan(repository)
    assert plan["unreachable_tables"] == []
    assert plan["all_joins_approved"] is True
    assert plan["all_joins_verified"] is False
    assert plan["join_edges"][0]["provenance"] == "user_verified"
    sql = "SELECT x_work.id FROM x_work JOIN x_group ON x_work.group_code = x_group.id"
    assert SQLValidator().validate(sql, _context(plan)).endswith("LIMIT 200;")

    forged = {**plan, "join_edges": [{**plan["join_edges"][0], "approved": False}]}
    with pytest.raises(SQLValidationError, match="has not been approved"):
        SQLValidator().validate(sql, _context(forged))


def test_approval_rechecks_schema_and_endpoint_changes_require_reapproval(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    with pytest.raises(ValueError, match="Source column"):
        _edge(repository, from_column="missing")
    item = _edge(repository)
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    edited = repository.update(item["relationship_id"], {"from_column": "shop_code"}, schema=SCHEMA)
    assert edited["status"] == "pending"
    assert repository.approved() == []
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    changed_schema = {"tables": [
        {**SCHEMA["tables"][0], "columns": [
            {**col, "datatype": "varchar" if col["name"] == "shop_code" else col["datatype"]}
            for col in SCHEMA["tables"][0]["columns"]]},
        SCHEMA["tables"][1],
    ]}
    repository.validate_against_schema(changed_schema)
    assert repository.approved() == []
    assert repository.all()[0]["status"] == "needs_review"


def test_approved_relationship_purpose_change_requires_reapproval(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    item = _edge(repository)
    approved = repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    assert approved["status"] == "approved"
    changed = repository.update(
        item["relationship_id"],
        {"purpose": "equipment assigned to a repair group"},
        schema=SCHEMA,
    )
    assert changed["status"] == "pending"
    assert "approval is required again" in changed["review_reason"]
    assert repository.approved() == []


def test_join_signedness_mismatch_needs_review(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    schema = {**SCHEMA, "tables": [
        {**SCHEMA["tables"][0], "columns": [
            {**col, "full_type": "int unsigned" if col["name"] == "group_code" else "int"}
            for col in SCHEMA["tables"][0]["columns"]]},
        {**SCHEMA["tables"][1], "columns": [
            {**col, "full_type": "int"} for col in SCHEMA["tables"][1]["columns"]]},
    ]}
    with pytest.raises(ValueError, match="signedness"):
        repository.create({"from_table": "x_work", "from_column": "group_code",
                           "to_table": "x_group", "to_column": "id",
                           "description": "Proposed relationship"}, schema=schema)


def test_composite_approved_key_must_be_complete_and_not_bypassable(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    item = _edge(repository, from_columns=["group_code", "shop_code"],
                 to_columns=["id", "shop_id"])
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    context = _context(_plan(repository))
    base = "SELECT x_work.id FROM x_work JOIN x_group ON "
    complete = base + "x_work.group_code = x_group.id AND x_work.shop_code = x_group.shop_id"
    SQLValidator().validate(complete, context)
    with pytest.raises(SQLValidationError, match="incomplete"):
        SQLValidator().validate(base + "x_work.group_code = x_group.id", context)
    with pytest.raises(SQLValidationError, match="only the approved key equalities"):
        SQLValidator().validate(complete + " OR 1 = 1", context)
    with pytest.raises(SQLValidationError, match="only approved column-to-column equalities"):
        SQLValidator().validate(complete + " AND 1 = 1", context)


def test_ambiguous_approved_paths_fail_instead_of_chosing_one():
    schema = {"database": "workshop", "tables": [
        {"name": name, "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}
        for name in ("a", "b", "c", "d")
    ]}
    pairs = [("a", "b"), ("b", "d"), ("a", "c"), ("c", "d")]
    approved = [{"relationship_id": str(i), "from_table": left, "from_column": "id",
                 "to_table": right, "to_column": "id", "status": "approved"}
                for i, (left, right) in enumerate(pairs)]
    finder = JoinPathFinder(RelationshipBuilder(schema, approved))
    plan = finder.find_multi_table_join(["a", "d"], allow_semantic_fallback=True,
                                       fail_on_ambiguous_paths=True)
    assert plan["join_edges"] == []
    assert plan["ambiguous_paths"][0]["reason"] == "multiple_equal_approved_relationship_paths"


def test_real_fk_still_wins_over_approved_semantic_edge(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    item = _edge(repository)
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    schema = {**SCHEMA, "tables": [
        {**SCHEMA["tables"][0], "foreign_keys": [{
            "table": "x_work", "column": "group_code", "references_table": "x_group",
            "references_column": "id", "constraint_name": "fk_work_group",
        }]}, SCHEMA["tables"][1],
    ]}
    plan = JoinPathFinder(RelationshipBuilder(schema, repository.approved())).find_multi_table_join(
        ["x_work", "x_group"], allow_semantic_fallback=True, fail_on_ambiguous_paths=True,
    )
    assert plan["all_joins_verified"] is True
    assert plan["semantic_edges_used"] == 0


def test_api_requires_separate_approval_and_rejects_unknown_columns(tmp_path):
    from flask import Flask
    from types import SimpleNamespace
    from routes.knowledge import bp

    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    app = Flask(__name__)
    app.extensions["services"] = {
        "semantic_relationships": repository,
        "cache": SimpleNamespace(schema=SCHEMA),
        "refresh_runtime_context": lambda: None,
    }
    app.register_blueprint(bp)
    client = app.test_client()
    payload = {"from_table": "x_work", "from_column": "group_code", "to_table": "x_group",
               "to_column": "id", "description": "DBA reviewed work-to-group business key",
               "status": "approved"}
    created = client.post("/api/semantic-relationships", json=payload)
    assert created.status_code == 201
    entry = created.get_json()["relationship"]
    assert entry["status"] == "pending"
    assert repository.approved() == []
    approved = client.put(f"/api/semantic-relationships/{entry['relationship_id']}", json={"status": "approved"})
    assert approved.status_code == 200
    assert len(repository.approved()) == 1
    bad = client.post("/api/semantic-relationships", json={**payload, "from_column": "missing"})
    assert bad.status_code == 400


def test_validator_rejects_sql_that_omits_required_join_table():
    schema = {"database": "workshop", "tables": [
        {"name": name, "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}
        for name in ("a", "b", "c")
    ]}
    semantic = [{"relationship_id": name, "from_table": left, "from_column": "id",
                 "to_table": right, "to_column": "id", "status": "approved"}
                for name, left, right in (("ab", "a", "b"), ("bc", "b", "c"))]
    plan = JoinPathFinder(RelationshipBuilder(schema, semantic)).find_multi_table_join(
        ["a", "c"], allow_semantic_fallback=True, fail_on_ambiguous_paths=True,
    )
    context = {"selected_tables": plan["included_tables"],
               "tables_by_name": {table["name"]: table for table in schema["tables"]},
               "join_plan": plan}
    with pytest.raises(SQLValidationError, match="omitted required join-plan table"):
        SQLValidator().validate("SELECT a.id FROM a JOIN b ON a.id = b.id", context)


def test_query_plan_accepts_only_the_approved_semantic_path(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    item = _edge(repository)
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    plan = _plan(repository)
    context = {"question": "show x_work with x_group", "selected_tables": ["x_work", "x_group"],
               "scored_tables": [{"table": "x_work", "matches": ["table_name: x_work"]},
                                 {"table": "x_group", "matches": ["table_name: x_group"]}],
               "join_plan": plan, "tables_by_name": _context(plan)["tables_by_name"]}
    approved_plan = QueryPlanBuilder(SCHEMA).build(context)
    assert approved_plan.status == "ready"
    assert approved_plan.verified_join_path == []
    assert len(approved_plan.approved_join_path) == 1
    unapproved = {**context, "join_plan": {**plan, "all_joins_approved": False}}
    assert QueryPlanBuilder(SCHEMA).build(unapproved).status == "failed"


def test_opaque_aliases_and_approved_join_reach_deterministic_plan(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "relationships.json", database_id="workshop")
    item = _edge(repository)
    repository.update(item["relationship_id"], {"status": "approved"}, schema=SCHEMA)
    meanings = {
            "x_work": {"definition": "repair equipment currently in the workshop", "aliases": ["equipment"]},
            "x_group": {"definition": "equipment ownership group", "aliases": ["group"]},
    }
    def build(relationships):
        generator = ContextGenerator(
            SCHEMA, {}, {"tables": {}}, {"rankings": []},
            JoinPathFinder(RelationshipBuilder(SCHEMA, relationships)),
            top_n=2, defined_knowledge=meanings,
        )
        return QueryContextBuilder(generator, query_plan_builder=QueryPlanBuilder(SCHEMA)).build(
            "Show equipment by group"
        )
    without_join = build([])
    context = build(repository.approved())
    assert without_join["query_plan"]["status"] == "failed"
    assert set(context["selected_tables"]) == {"x_work", "x_group"}
    assert context["join_plan"]["all_joins_approved"] is True
    assert context["join_plan"]["all_joins_verified"] is False
    assert context["query_plan"]["status"] == "ready"
    assert "human-approved non-FK" in context["prompt"]


def test_context_budget_does_not_silently_drop_grounded_table(monkeypatch):
    schema = {"database": "workshop", "tables": [
        {"name": str(i), "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}
        for i in range(5)
    ]}
    generator = ContextGenerator(schema, {}, {"tables": {}}, {"rankings": []},
                                 JoinPathFinder(RelationshipBuilder(schema)), top_n=2)
    generator.find_relevant_tables = lambda question: [{"table": "0", "matches": []}, {"table": "4", "matches": []}]
    generator._demote_structural_bridges = lambda selected, question: selected
    generator._demote_comparison_dimensions = lambda selected, question: selected
    generator.join_finder.find_multi_table_join = lambda *args, **kwargs: {
        "base_table": "0", "requested_tables": ["0", "4"],
        "included_tables": ["0", "1", "2", "3", "4"], "join_edges": [],
        "unreachable_tables": [], "all_joins_approved": True,
    }
    from config.settings import settings
    monkeypatch.setattr(settings, "MAX_FINAL_TABLES", 4)
    result = QueryContextBuilder(generator).build("show 0 with 4")
    assert result["query_plan"]["status"] == "failed"
    assert result["selected_tables"] == ["0", "1", "2", "3", "4"]
    assert result["prompt"] == ""
