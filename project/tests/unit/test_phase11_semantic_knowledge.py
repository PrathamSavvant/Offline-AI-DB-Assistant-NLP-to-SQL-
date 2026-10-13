from pathlib import Path

from core.defined_knowledge import DefinedKnowledgeRepository
from core.predicate_planner import PredicatePlanner
from core.rag_layer import LexicalRetriever
from core.semantic_context_builder import SemanticContextBuilder
from core.value_resolver import ValueResolver


SCHEMA = {"tables": [
    {"name": "work_equipment", "columns": [{"name": "id", "datatype": "int"}, {"name": "status", "datatype": "varchar"}, {"name": "group_id", "datatype": "int"}]},
    {"name": "equipments", "columns": [{"name": "id", "datatype": "int"}, {"name": "status", "datatype": "varchar"}]},
    {"name": "groups", "columns": [{"name": "id", "datatype": "int"}, {"name": "name", "datatype": "varchar"}]},
]}


def _repository(tmp_path: Path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined_knowledge.json")
    repo.update_table("work_equipment", {
        "business_entity": "Work Equipment", "aliases": ["equipment", "assigned equipment"],
        "intended_usage": ["equipment workflow", "equipment status"],
        "exclusions": ["historical repair records"],
    })
    repo.update_column("work_equipment", "status", {
        "business_meaning": "Current workflow status", "aliases": ["workflow status"],
        "important_values": {"waiting for collection": "AWAITING COLLECTION", "under repair": "UNDER REPAIR"},
    })
    return repo


def test_approved_semantics_promote_and_resolve_before_llm(tmp_path):
    repo = _repository(tmp_path)
    retriever = LexicalRetriever({t["name"]: t for t in SCHEMA["tables"]}, {}, {}, defined_knowledge=repo.all())
    assert retriever.score_table("work_equipment", "equipment waiting for collection") ["score"] > retriever.score_table("equipments", "equipment waiting for collection")["score"]
    resolver = ValueResolver(SCHEMA, defined_knowledge=repo)
    context = {"question": "equipment waiting for collection in TL group", "selected_tables": ["work_equipment", "groups"]}
    context["resolved_values"] = [item.to_dict() for item in resolver.resolve(context)]
    filters = PredicatePlanner(SCHEMA, value_resolver=resolver).plan(context)["filters"]
    assert any(item["table"] == "work_equipment" and item["column"] == "status" and item["value"] == "AWAITING COLLECTION" for item in filters)
    assert not any(item["value"] == "Waiting For Collection" for item in filters)


def test_semantic_context_is_compact_and_pending_is_inert(tmp_path):
    repo = _repository(tmp_path)
    context = {"question": "how many under repair", "selected_tables": ["work_equipment"]}
    semantic = SemanticContextBuilder(SCHEMA, defined_knowledge=repo).build(context)
    assert semantic["relevant_columns"] == {"work_equipment": ["status"]}
    assert semantic["approved_predicates"][0]["value"] == "UNDER REPAIR"
    empty = DefinedKnowledgeRepository(tmp_path / "pending_only.json")
    assert SemanticContextBuilder(SCHEMA, defined_knowledge=empty).build(context)["approved_predicates"] == []
