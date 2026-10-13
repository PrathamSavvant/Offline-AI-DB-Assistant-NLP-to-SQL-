"""Deterministic query-trace analysis for knowledge feedback (Phase 10)."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List

class KnowledgeFailureAnalyzer:
    def __init__(self, pending_repository): self.pending = pending_repository
    def analyze(self, context: Dict[str, Any]) -> Dict[str, Any]:
        question = str(context.get("question") or ""); impact = context.get("knowledge_impact", {}); manifest = context.get("predicate_manifest", {}); trace = context.get("retrieval_trace") or {}; findings: List[Dict[str, Any]] = []
        if manifest.get("ambiguities"): findings.append({"classification": "PREDICATE ISSUE", "kind": "predicate_ambiguity", "evidence": ["Predicate planner reported multiple candidates."], "action": "Review or define an approved predicate mapping."})
        if not context.get("selected_tables"): findings.append({"classification": "RETRIEVAL ISSUE", "kind": "no_context", "evidence": ["No tables were selected by retrieval."], "action": "Review aliases, phrases, and table knowledge."})
        if trace.get("relationship_expansion", {}).get("unreachable_tables") or context.get("join_plan", {}).get("unreachable_tables"): findings.append({"classification": "JOIN ISSUE", "kind": "unreachable_join", "evidence": ["Selected tables are not connected by known relationships."], "action": "Review relationship knowledge; do not infer a join automatically."})
        if not impact.get("knowledge_effective") and context.get("selected_tables"): findings.append({"classification": "KNOWLEDGE ISSUE", "kind": "no_approved_knowledge_used", "evidence": ["No approved knowledge contributed to this query's retrieval or predicates."], "action": "Review aliases, phrases, and categorical predicate suggestions."})
        if impact.get("knowledge_conflicts"): findings.append({"classification": "KNOWLEDGE ISSUE", "kind": "approved_knowledge_conflict", "evidence": ["Approved predicate mappings conflict."], "action": "Review competing mappings; retain ambiguity until a user resolves it."})
        diagnostic = {"at": datetime.now(timezone.utc).isoformat(), "question": question[:500], "retrieved_tables": context.get("retrieved_tables", []), "selected_tables": context.get("selected_tables", []), "predicate_status": manifest.get("status"), "ambiguity_count": len(manifest.get("ambiguities", [])), "knowledge_impact": {"effective": impact.get("knowledge_effective", False), "contribution": impact.get("knowledge_score_contribution", 0)}, "findings": findings}
        self.pending.add_query_diagnostic(diagnostic); created = []
        if any(x["kind"] == "no_approved_knowledge_used" for x in findings):
            for table in context.get("selected_tables", [])[:1]:
                recommendation = {"type": "feedback_review", "table": table, "column": "", "phrase": question.lower()[:120], "evidence": ["Query selected this table but no approved knowledge contributed."], "source": "QUERY_FEEDBACK", "confidence": "MEDIUM", "impact": "May improve future retrieval and predicate interpretation."}
                if self.pending.add_feedback_recommendation(recommendation): created.append(recommendation)
        return {"findings": findings, "recommendations_created": created, "classification": findings[0]["classification"] if findings else "UNKNOWN"}
