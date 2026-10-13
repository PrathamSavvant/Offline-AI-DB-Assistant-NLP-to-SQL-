from core.join_path_finder import JoinPathFinder
from core.relationship_builder import RelationshipBuilder
from core.semantic_relationships import SemanticRelationshipRepository


SCHEMA = {"tables": [
    {"name": "equipments", "columns": [{"name": "id"}, {"name": "group_code"}], "foreign_keys": []},
    {"name": "groups", "columns": [{"name": "id"}], "foreign_keys": []},
]}


def _create(repository, status="approved"):
    return repository.create({"from_table": "equipments", "from_column": "group_code", "to_table": "groups", "to_column": "id", "description": "Verified test relationship", "status": status})


def test_persistence_and_database_isolation(tmp_path):
    path = tmp_path / "semantic_relationships.json"
    item = _create(SemanticRelationshipRepository(path, database_id="db-a"))
    assert SemanticRelationshipRepository(path, database_id="db-a").approved()[0]["relationship_id"] == item["relationship_id"]
    assert SemanticRelationshipRepository(path, database_id="db-b").all() == []


def test_only_approved_edges_join_and_preserve_provenance(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "semantic.json", database_id="db-a")
    approved = _create(repository)
    _create(repository, "pending")
    finder = JoinPathFinder(RelationshipBuilder(SCHEMA, repository.approved()))
    plan = finder.find_multi_table_join(["equipments", "groups"])
    assert plan["unreachable_tables"] == []
    assert plan["join_edges"][0]["relationship_type"] == "semantic"
    assert plan["join_edges"][0]["provenance"] == "user_verified"
    repository.update(approved["relationship_id"], {"status": "disabled"})
    assert JoinPathFinder(RelationshipBuilder(SCHEMA, repository.approved())).find_multi_table_join(["equipments", "groups"])["unreachable_tables"] == ["groups"]


def test_stale_columns_are_retained_and_marked_for_review(tmp_path):
    repository = SemanticRelationshipRepository(tmp_path / "semantic.json", database_id="db-a")
    item = _create(repository)
    repository.validate_against_schema({"tables": [{"name": "equipments", "columns": [{"name": "id"}]}]})
    saved = next(entry for entry in repository.all() if entry["relationship_id"] == item["relationship_id"])
    assert saved["status"] == "needs_review"
    assert saved["review_reason"] == "Source column no longer exists"


def test_matching_column_names_do_not_create_edges_without_a_record():
    plan = JoinPathFinder(RelationshipBuilder(SCHEMA)).find_multi_table_join(["equipments", "groups"])
    assert plan["unreachable_tables"] == ["groups"]
