from core.knowledge_impact import build_knowledge_impact

def test_knowledge_impact_reports_approved_signals_and_predicates():
    context = {"retrieval_trace": {"reranking": [{"table": "assets", "matches": ["approved_alias:equipment (+9.0)", "approved_predicate:out of service (+10.0)"]}]}, "predicate_manifest": {"filters": [{"table": "assets", "column": "state", "value": "OUT", "source": "Knowledge Layer"}], "ambiguities": []}}
    impact = build_knowledge_impact(context)
    assert impact["knowledge_effective"] is True
    assert impact["approved_alias_matches"] == 1
    assert impact["approved_predicate_matches"] == 1
    assert impact["predicates_resolved_by_knowledge"][0]["column"] == "state"
