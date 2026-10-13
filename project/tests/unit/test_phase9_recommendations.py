from core.defined_knowledge import DefinedKnowledgeRepository
from core.knowledge_recommendations import KnowledgeRecommendationEngine

SCHEMA = {"tables": [{"name": "asset_states", "approx_row_count": 1000, "columns": [{"name": "id", "datatype": "int"}, {"name": "state", "full_type": "enum('READY','OUT')"}]}]}

def test_recommendations_are_pending_and_evidence_backed(tmp_path):
    repo = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    engine = KnowledgeRecommendationEngine(SCHEMA, {"relationships": []}, {"rankings": [{"table": "asset_states", "score": .5}]}, repo)
    recommendations = engine.recommendations("asset_states")
    assert all(item["status"] == "PENDING" and item["evidence"] for item in recommendations)
    assert any(item["type"] == "important_value" and item["suggested_value"] == "READY" for item in recommendations)
    assert any(item["type"] == "predicate" for item in recommendations)

def test_priority_is_deterministic_and_missing_knowledge_matters(tmp_path):
    repo = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    engine = KnowledgeRecommendationEngine(SCHEMA, {"relationships": []}, {"rankings": [{"table": "asset_states", "score": .5}]}, repo)
    first = engine.prioritized_tables()[0]
    repo.update_table("asset_states", {"definition": "Events that record an asset state."})
    second = engine.prioritized_tables()[0]
    assert first["priority"]["score"] > second["priority"]["score"]
