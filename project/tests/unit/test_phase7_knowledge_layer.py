from core.defined_knowledge import DefinedKnowledgeRepository
from core.inverted_index import InvertedTableIndex
from core.predicate_planner import PredicatePlanner
from core.rag_layer import LexicalRetriever
from core.value_resolver import ValueResolver


SCHEMA = {"tables": [
    {"name": "asset_events", "columns": [{"name": "id"}, {"name": "state"}]},
    {"name": "audit_events", "columns": [{"name": "id"}, {"name": "state"}]},
]}


def test_approved_alias_is_indexed_and_outranks_generic_tags(tmp_path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repo.update_table("asset_events", {"aliases": ["equipment timeline"], "approval_status": "USER_VERIFIED"})
    index = InvertedTableIndex(); index.build(SCHEMA, defined_knowledge=repo.all())
    assert "asset_events" in index.get_candidate_tables("equipment timeline")
    retriever = LexicalRetriever({t["name"]: t for t in SCHEMA["tables"]}, {}, {"asset_events": {"business_tags": ["equipment"]}, "audit_events": {"business_tags": ["equipment"]}}, inverted_index=index, defined_knowledge=repo.all())
    assert retriever.score_table("asset_events", "equipment timeline")["score"] > retriever.score_table("audit_events", "equipment timeline")["score"]


def test_approved_predicate_is_used_and_conflict_is_visible(tmp_path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repo.upsert_predicate("asset_events", {"phrase": "out of service", "column": "state", "value": "OUT", "semantic_type": "STATUS"})
    repo.upsert_predicate("audit_events", {"phrase": "out of service", "column": "state", "value": "DISABLED", "semantic_type": "STATUS"})
    resolver = ValueResolver(SCHEMA, defined_knowledge=repo)
    manifest = PredicatePlanner(SCHEMA, value_resolver=resolver).plan({"question": "out of service", "selected_tables": ["asset_events", "audit_events"]})
    assert any(item["reason"] == "conflicting_approved_predicate_mappings" for item in manifest["ambiguities"])
    assert repo.conflicts()[0]["phrase"] == "out of service"


def test_column_knowledge_and_history_are_database_scoped(tmp_path):
    left = DefinedKnowledgeRepository(tmp_path / "one.json"); right = DefinedKnowledgeRepository(tmp_path / "two.json")
    left.update_column("asset_events", "state", {"business_meaning": "current operational condition"})
    assert left.table("asset_events")["columns"]["state"]["business_meaning"]
    assert right.table("asset_events").get("columns", {}) == {}
    assert left.history("asset_events")
