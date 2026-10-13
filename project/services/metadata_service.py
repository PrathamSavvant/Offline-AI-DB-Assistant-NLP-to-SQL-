"""Cache-only schema and relationship reads. No dependency on an LLM provider."""
from typing import Any, Dict
from core.db_context import db_context

class MetadataService:
    def __init__(self, cache, dictionary): self.cache, self.dictionary = cache, dictionary
    def metadata(self) -> Dict[str, Any]: return {"total_tables": len(self.cache.tables), "database_name": db_context.active_database, "tables": [{"name": t["name"], "column_count": len(t.get("columns", []))} for t in self.cache.tables]}
    def schema(self): return {"tables": [{"name": t["name"], "columns": [c["name"] for c in t.get("columns", [])], "primary_keys": t.get("primary_keys", []), "foreign_keys": t.get("foreign_keys", [])} for t in self.cache.tables]}
    def table(self, name):
        table = self.cache.get_table(name)
        return {"error": f"Table '{name}' not found in schema."} if not table else {"table": table["name"], "columns": table.get("columns", []), "primary_keys": table.get("primary_keys", []), "foreign_keys": table.get("foreign_keys", []), "approx_row_count": table.get("approx_row_count", 0)}
    def relationships(self, name=None):
        rels = self.cache.get_relationships_for_table(name) if name else self.cache.relationships.get("relationships", [])
        return ({"table": name, "relationships": rels, "relationship_count": len(rels)} if name else {"relationships": rels, "total_relationships": len(rels)})
    def dictionary_entries(self):
        entries = [{"abbreviation": key, **entry} for key, entry in sorted(self.dictionary.get_all().items())]
        return {"entries": entries, "total": len(entries)}
