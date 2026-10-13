"""Offline, schema-driven recommendations for authoring approved knowledge."""
from __future__ import annotations
import re
from typing import Any, Dict, List

CONFIG = {"row_count_weight": 0.35, "relationship_weight": 0.35, "missing_weight": 0.30}

def words(value: str) -> List[str]: return re.findall(r"[a-z0-9]+", str(value).replace("_", " ").lower())

class KnowledgeRecommendationEngine:
    def __init__(self, schema: Dict[str, Any], relationships: Dict[str, Any], ranking: Dict[str, Any], defined: Any):
        self.schema, self.defined = schema, defined
        self.relationships = relationships.get("relationships", [])
        self.ranking = {x.get("table"): x for x in ranking.get("rankings", [])}

    def completeness(self, table: Dict[str, Any], knowledge: Dict[str, Any]) -> Dict[str, Any]:
        cols = table.get("columns", []); known_cols = knowledge.get("columns", {})
        column_coverage = sum(bool(known_cols.get(c.get("name"), {}).get("business_meaning")) for c in cols)
        checks = {"definition": bool(knowledge.get("definition")), "business_identity": bool(knowledge.get("business_name")), "aliases": bool(knowledge.get("aliases") or knowledge.get("synonyms")), "phrases": bool(knowledge.get("common_phrases") or knowledge.get("query_phrases")), "column_meanings": bool(cols) and column_coverage == len(cols), "vocabulary": bool(knowledge.get("abbreviations")), "important_values": any(x.get("important_values") for x in known_cols.values()), "predicates": bool(knowledge.get("predicate_mappings")), "exclusions": bool(knowledge.get("exclusions")), "relationships": any(r.get("from_table") == table.get("name") or r.get("to_table") == table.get("name") for r in self.relationships)}
        return {"score": round(sum(checks.values()) / len(checks) * 100), "checks": checks, "columns": {"covered": column_coverage, "total": len(cols)}}

    def priority(self, table: Dict[str, Any], knowledge: Dict[str, Any]) -> Dict[str, Any]:
        name = table["name"]; incoming = sum(r.get("to_table") == name for r in self.relationships); outgoing = sum(r.get("from_table") == name for r in self.relationships)
        importance = float(self.ranking.get(name, {}).get("score", 0)); completeness = self.completeness(table, knowledge)["score"]
        rows = int(table.get("approx_row_count") or 0); score = round(importance * 100 + (incoming + outgoing) * 5 + min(20, len(str(max(rows, 1))) * 3) + (100 - completeness) * CONFIG["missing_weight"], 2)
        return {"score": score, "tier": "HIGH" if score >= 75 else "MEDIUM" if score >= 40 else "LOW", "evidence": {"row_count": rows, "incoming_relationships": incoming, "outgoing_relationships": outgoing, "schema_importance": importance, "completeness": completeness}}

    def recommendations(self, table_name: str) -> List[Dict[str, Any]]:
        table = next((x for x in self.schema.get("tables", []) if x.get("name") == table_name), None)
        if not table: return []
        k = self.defined.table(table_name); result = []; priority = self.priority(table, k)
        def add(kind, value, evidence, confidence="MEDIUM", payload=None, impact="May improve retrieval and query interpretation."):
            result.append({"id": f"{kind}:{value}", "type": kind, "suggested_value": value, "evidence": evidence, "confidence": confidence, "status": "PENDING", "source": "SYSTEM", "impact": impact, "payload": payload or {}, "priority": priority})
        if not k.get("business_name"): add("business_name", table_name.replace("_", " ").title(), ["Derived from table identifier; review business wording."], "LOW")
        if not k.get("definition"): add("table_definition", table_name.replace("_", " "), ["No approved table definition exists.", f"Cached row count: {table.get('approx_row_count', 0)}."], "LOW")
        for col in table.get("columns", []):
            name = col.get("name", ""); column_k = k.get("columns", {}).get(name, {})
            if name and not column_k.get("business_meaning") and name not in {"id", "created_at", "updated_at"}:
                add("column_meaning", name.replace("_", " "), [f"Column `{name}` has no approved business meaning.", f"Schema type: {col.get('datatype') or col.get('full_type') or 'unknown'}."], "LOW", {"column": name})
            full_type = str(col.get("full_type") or "")
            values = re.findall(r"'([^']+)'", full_type) if full_type.lower().startswith("enum") else []
            for value in values:
                if value not in column_k.get("important_values", {}):
                    add("important_value", value, [f"Enum value declared by schema for `{name}`."], "HIGH", {"column": name, "value": value}, "Can support a reviewed value meaning or predicate.")
                    if any(token in words(name) for token in ("status", "state", "type", "category")):
                        add("predicate", value.lower(), [f"Categorical value `{value}` is declared for `{name}`.", "Phrase is a reviewable starting point, not an approved mapping."], "MEDIUM", {"column": name, "value": value, "phrase": value.lower()}, "Can resolve this phrase directly after approval.")
        return result

    def prioritized_tables(self) -> List[Dict[str, Any]]:
        rows = []
        for table in self.schema.get("tables", []):
            k = self.defined.table(table["name"]); rows.append({"table": table["name"], "completeness": self.completeness(table, k), "priority": self.priority(table, k), "recommendation_count": len(self.recommendations(table["name"]))})
        return sorted(rows, key=lambda x: (-x["priority"]["score"], x["table"]))
