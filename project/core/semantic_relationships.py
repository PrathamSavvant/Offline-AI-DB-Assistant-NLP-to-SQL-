"""Explicit, database-scoped semantic relationships for verified ERP joins."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context

VALID_STATUSES = {"pending", "approved", "rejected", "disabled", "needs_review"}
INTEGER_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SemanticRelationshipRepository:
    """Persistent human-verified edges; never infers a relationship."""

    def __init__(self, path: Optional[Path] = None, database_id: Optional[str] = None):
        self._custom_path, self.database_id = path, database_id or db_context.database_id
        self._data: Dict[str, Any] = {"version": 1, "database_id": self.database_id, "relationships": []}
        self.reload()

    @property
    def path(self) -> Path:
        return self._custom_path or db_context.get_knowledge_dir() / "semantic_relationships.json"

    def reload(self) -> None:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
            if raw.get("database_id") == self.database_id:
                self._data = raw
        self._data.setdefault("version", 1)
        self._data["database_id"] = self.database_id
        self._data.setdefault("relationships", [])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def all(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self._data["relationships"]]

    def approved(self) -> List[Dict[str, Any]]:
        return [item for item in self.all() if item.get("status") == "approved" and item.get("database_id") == self.database_id]

    @staticmethod
    def _column_pairs(data: Dict[str, Any]) -> tuple[list[str], list[str]]:
        left = data.get("from_columns")
        right = data.get("to_columns")
        if left is None:
            left = [data.get("from_column")]
        if right is None:
            right = [data.get("to_column")]
        if (not isinstance(left, list) or not isinstance(right, list) or not left
                or len(left) != len(right) or not all(isinstance(name, str) and name.strip() for name in left + right)):
            raise ValueError("Relationship needs matching non-empty source and target column lists")
        left = [name.strip() for name in left]
        right = [name.strip() for name in right]
        if len(set(left)) != len(left) or len(set(right)) != len(right):
            raise ValueError("Relationship column lists cannot contain duplicates")
        return left, right

    @staticmethod
    def _schema_issue(item: Dict[str, Any], schema: Dict[str, Any]) -> str:
        tables = {table.get("name"): {col.get("name"): col for col in table.get("columns", [])}
                  for table in schema.get("tables", [])}
        source, target = item.get("from_table"), item.get("to_table")
        if source not in tables:
            return "Source table no longer exists"
        try:
            left, right = SemanticRelationshipRepository._column_pairs(item)
        except ValueError as exc:
            return str(exc)
        for source_col in left:
            if source_col not in tables[source]:
                return "Source column no longer exists"
        if target not in tables:
            return "Target table no longer exists"
        for source_col, target_col in zip(left, right):
            if target_col not in tables[target]:
                return "Target column no longer exists"
            source_type = str(tables[source][source_col].get("datatype") or "").lower()
            target_type = str(tables[target][target_col].get("datatype") or "").lower()
            if source_type and target_type and source_type != target_type and not ({source_type, target_type} <= INTEGER_TYPES):
                return "Join column data types differ; DBA review is required"
            source_full = str(tables[source][source_col].get("full_type") or "").lower()
            target_full = str(tables[target][target_col].get("full_type") or "").lower()
            if source_full and target_full and ("unsigned" in source_full) != ("unsigned" in target_full):
                return "Join column signedness differs; DBA review is required"
        return ""

    def create(self, data: Dict[str, Any], schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        required = ("from_table", "to_table", "description")
        missing = [key for key in required if not str(data.get(key) or "").strip()]
        if missing: raise ValueError("Missing required fields: " + ", ".join(missing))
        left, right = self._column_pairs(data)
        status = str(data.get("status") or "pending").lower()
        if status not in VALID_STATUSES: raise ValueError("Invalid relationship status")
        now = _now()
        item = {"relationship_id": str(uuid.uuid4()), "database_id": self.database_id,
                "from_table": str(data["from_table"]).strip(), "from_column": left[0], "from_columns": left,
                "to_table": str(data["to_table"]).strip(), "to_column": right[0], "to_columns": right,
                "relationship_type": "semantic", "status": status, "source": "user_verified",
                "description": str(data["description"]).strip(),
                "purpose": str(data.get("purpose") or data["description"]).strip(),
                "cardinality": str(data.get("cardinality") or "unknown").strip(),
                "scope": str(data.get("scope") or "all").strip(),
                "created_at": now, "updated_at": now, "review_reason": ""}
        if schema:
            issue = self._schema_issue(item, schema)
            if issue: raise ValueError(issue)
        self._data["relationships"].append(item); self._save(); return dict(item)

    def update(self, relationship_id: str, data: Dict[str, Any], schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        for item in self._data["relationships"]:
            if item.get("relationship_id") == relationship_id:
                candidate = dict(item)
                for key in ("from_table", "to_table", "description", "purpose", "cardinality", "scope"):
                    if key in data: candidate[key] = str(data[key]).strip()
                if "description" in data and "purpose" not in data and item.get("purpose") == item.get("description"):
                    candidate["purpose"] = candidate["description"]
                for key in ("from_column", "to_column", "from_columns", "to_columns"):
                    if key in data: candidate[key] = data[key]
                if "from_column" in data and "from_columns" not in data:
                    candidate["from_columns"] = [data["from_column"]]
                if "to_column" in data and "to_columns" not in data:
                    candidate["to_columns"] = [data["to_column"]]
                left, right = self._column_pairs(candidate)
                candidate.update(from_column=left[0], from_columns=left, to_column=right[0], to_columns=right)
                candidate["status"] = str(data.get("status", item.get("status"))).strip().lower()
                if candidate["status"] not in VALID_STATUSES: raise ValueError("Invalid relationship status")
                if not candidate.get("from_table") or not candidate.get("to_table") or not candidate.get("description"):
                    raise ValueError("Relationship needs both tables and a description")
                if schema:
                    issue = self._schema_issue(candidate, schema)
                    if issue: raise ValueError(issue)
                changed_authority = any(
                    candidate.get(key) != item.get(key)
                    for key in (
                        "from_table", "from_columns", "to_table", "to_columns",
                        "description", "purpose", "cardinality", "scope",
                    )
                )
                if changed_authority and item.get("status") == "approved":
                    candidate["status"] = "pending"
                    candidate["review_reason"] = "Join definition changed; approval is required again"
                elif candidate["status"] == "approved":
                    candidate["review_reason"] = ""
                candidate["updated_at"] = _now()
                item.update(candidate); self._save(); return dict(item)
        raise KeyError(relationship_id)

    def validate_against_schema(self, schema: Dict[str, Any]) -> List[Dict[str, Any]]:
        changed = []
        for item in self._data["relationships"]:
            if item.get("database_id") != self.database_id: continue
            reason = self._schema_issue(item, schema)
            if reason and item.get("status") != "needs_review":
                item["status"], item["review_reason"], item["updated_at"] = "needs_review", reason, _now(); changed.append(dict(item))
        if changed: self._save()
        return changed
