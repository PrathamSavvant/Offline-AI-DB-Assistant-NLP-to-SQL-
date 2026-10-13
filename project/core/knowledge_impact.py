"""Phase 8 diagnostics for approved-knowledge influence on a query."""
from __future__ import annotations
from typing import Any, Dict, List

def build_knowledge_impact(context: Dict[str, Any]) -> Dict[str, Any]:
    trace = context.get("retrieval_trace") or {}; reranking = trace.get("reranking", []) if isinstance(trace, dict) else []
    entries: List[Dict[str, Any]] = []; promoted = []; contribution = 0.0
    prefixes = {"approved_alias:": "alias", "approved_phrase:": "phrase", "approved_column:": "column", "approved_predicate:": "predicate", "defined_synonym:": "synonym", "defined_abbr:": "abbreviation", "manual_definition:": "definition"}
    for record in reranking:
        for match in record.get("matches", []):
            for prefix, kind in prefixes.items():
                if match.startswith(prefix):
                    table = record.get("table", ""); entries.append({"table": table, "type": kind, "matched_text": match[len(prefix):].split(" (+", 1)[0], "reason": "approved knowledge"})
                    try: contribution += float(match.rsplit("+", 1)[-1].rstrip(")"))
                    except ValueError: pass
                    if table not in promoted: promoted.append(table)
                    break
    predicates = [{"table": x.get("table"), "column": x.get("column"), "value": x.get("value"), "phrase": x.get("reason")} for x in context.get("predicate_manifest", {}).get("filters", []) if x.get("source") == "Knowledge Layer"]
    conflicts = [x for x in context.get("predicate_manifest", {}).get("ambiguities", []) if x.get("reason") == "conflicting_approved_predicate_mappings"]
    return {"knowledge_candidates_found": len(entries), "knowledge_entries_used": entries, "approved_alias_matches": sum(x["type"] == "alias" for x in entries), "approved_phrase_matches": sum(x["type"] == "phrase" for x in entries), "approved_column_matches": sum(x["type"] == "column" for x in entries), "approved_predicate_matches": sum(x["type"] == "predicate" for x in entries), "knowledge_score_contribution": round(contribution, 4), "tables_promoted_by_knowledge": promoted, "predicates_resolved_by_knowledge": predicates, "knowledge_conflicts": conflicts, "knowledge_entries_ignored": [], "knowledge_effective": bool(entries or predicates)}

class KnowledgeEffectivenessHarness:
    """LLM-free retrieval/planning runner and approved-knowledge A/B comparison."""
    def __init__(self, context_builder): self.context_builder = context_builder
    @staticmethod
    def _summary(context):
        return {"retrieved_tables": context.get("retrieved_tables", []), "reranked_tables": [x.get("table") for x in (context.get("retrieval_trace") or {}).get("reranking", [])], "selected_context": context.get("selected_tables", []), "resolved_predicates": context.get("predicate_manifest", {}).get("filters", []), "ambiguities": context.get("predicate_manifest", {}).get("ambiguities", []), "join_path": context.get("join_plan", {}), "knowledge_impact": context.get("knowledge_impact", {})}
    def run(self, question: str): return self._summary(self.context_builder.build(question))
    def compare(self, question: str):
        lexical = self.context_builder.generator.rag_layer.lexical_retriever; resolver = self.context_builder.value_resolver
        old_knowledge, old_resolver = lexical.defined_knowledge, getattr(resolver, "defined_knowledge", None)
        try:
            lexical.defined_knowledge = {}; resolver.defined_knowledge = None; without = self.run(question)
        finally: lexical.defined_knowledge = old_knowledge; resolver.defined_knowledge = old_resolver
        with_knowledge = self.run(question)
        return {"without_knowledge": without, "with_knowledge": with_knowledge, "ranking_changes": {"before": without["reranked_tables"], "after": with_knowledge["reranked_tables"]}, "table_promotion": [t for t in with_knowledge["selected_context"] if t not in without["selected_context"]], "predicate_changes": {"before": without["resolved_predicates"], "after": with_knowledge["resolved_predicates"]}, "ambiguity_changes": {"before": without["ambiguities"], "after": with_knowledge["ambiguities"]}, "knowledge_contribution": with_knowledge["knowledge_impact"]}
