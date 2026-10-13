from core.knowledge_feedback import KnowledgeFailureAnalyzer
from core.pending_knowledge import PendingKnowledgeRepository

def test_feedback_detects_knowledge_gap_and_deduplicates(tmp_path):
    pending = PendingKnowledgeRepository(tmp_path / "pending.json"); analyzer = KnowledgeFailureAnalyzer(pending)
    context = {"question": "find asset timelines", "selected_tables": ["asset_events"], "retrieved_tables": ["asset_events"], "predicate_manifest": {"status": "RESOLVED", "filters": [], "ambiguities": []}, "knowledge_impact": {"knowledge_effective": False, "knowledge_score_contribution": 0}, "retrieval_trace": {}, "join_plan": {}}
    first = analyzer.analyze(context); second = analyzer.analyze(context)
    assert first["classification"] == "KNOWLEDGE ISSUE"
    assert len(first["recommendations_created"]) == 1
    assert second["recommendations_created"] == []
    assert len(pending.query_diagnostics()) == 2

def test_feedback_does_not_claim_every_issue_is_knowledge(tmp_path):
    pending = PendingKnowledgeRepository(tmp_path / "pending.json"); analyzer = KnowledgeFailureAnalyzer(pending)
    result = analyzer.analyze({"question": "anything", "selected_tables": [], "predicate_manifest": {"ambiguities": []}, "knowledge_impact": {"knowledge_effective": False}, "retrieval_trace": {}, "join_plan": {}})
    assert result["classification"] == "RETRIEVAL ISSUE"
