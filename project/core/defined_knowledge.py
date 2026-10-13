"""User-owned, table-scoped Defined Knowledge storage.

This is an enhancement layer only. SchemaCache remains the structural truth;
the SQL pipeline does not read or depend on this repository.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.db_context import db_context


def _clean_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = value.split(",")
    return list(dict.fromkeys(item.strip() for item in (value or []) if str(item).strip()))


class DefinedKnowledgeRepository:
    """Persist approved, database-scoped business semantics per table.

    Pending discoveries deliberately never enter this repository.  Consumers
    can therefore treat every value returned here as explicit user knowledge.
    """

    def __init__(self, path: Optional[Path] = None):
        self._custom_path = path
        self._data: Dict[str, Any] = {"version": 3, "tables": {}, "legacy_unassigned_terms": {}, "history": []}
        self._load()

    @property
    def path(self) -> Path:
        return self._custom_path or db_context.get_knowledge_dir() / "defined_knowledge.json"

    def _load(self) -> None:
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as handle:
                self._data = json.load(handle)
        self._data["version"] = 3
        self._data.setdefault("tables", {})
        self._data.setdefault("legacy_unassigned_terms", {})
        self._data.setdefault("history", [])

    def reload(self) -> None:
        self._load()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2, ensure_ascii=False)
        temporary.replace(self.path)

    @staticmethod
    def _empty_table() -> Dict[str, Any]:
        # Keep an untouched table backward-compatible and minimal. Rich
        # semantic fields are added only when a user actually authors them.
        return {"definition": "", "synonyms": [], "abbreviations": {}}

    @classmethod
    def _normalise_table(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """Convert the short-lived verbose layout into the minimal model."""
        value = value or {}
        abbreviations = dict(value.get("abbreviations", {}))
        for token, old_term in value.get("business_terms", {}).items():
            abbreviations.setdefault(token.upper(), {
                "abbreviation": token.upper(), "meaning": old_term.get("meaning", old_term.get("business_term", "")),
                "column": old_term.get("maps_to_column", ""),
            })
        normalized = {
            "definition": str(value.get("definition") or value.get("description") or value.get("purpose") or "").strip(),
            "synonyms": _clean_list(value.get("synonyms", [])),
            "abbreviations": {
                str(token).upper(): {
                    "abbreviation": str((item if isinstance(item, dict) else {}).get("abbreviation") or token).upper(),
                    "meaning": str((item if isinstance(item, dict) else {"meaning": item}).get("meaning") or "").strip(),
                    "context": str((item if isinstance(item, dict) else {}).get("context") or "").strip(),
                    "column": str((item if isinstance(item, dict) else {}).get("column") or (item if isinstance(item, dict) else {}).get("maps_to_column") or "").strip(),
                }
                for token, item in abbreviations.items()
            },
        }
        for term in normalized["abbreviations"].values():
            if not term.get("context"):
                term.pop("context", None)
        rich_fields = {"business_name", "intended_usage", "exclusions", "aliases", "common_phrases", "query_phrases", "columns", "predicate_mappings", "record_grain", "record_role", "current_record", "source", "confidence", "approval_status", "created_at", "updated_at"}
        if not any(field in value for field in rich_fields):
            return normalized
        normalized.update({
            "business_entity": str(value.get("business_entity") or "").strip(),
            "business_name": str(value.get("business_name") or "").strip(),
            "intended_usage": _clean_list(value.get("intended_usage") or value.get("usage_notes") or []),
            "exclusions": _clean_list(value.get("exclusions") or value.get("do_not_use_for") or []), "aliases": _clean_list(value.get("aliases", [])),
            "common_phrases": _clean_list(value.get("common_phrases", [])), "query_phrases": _clean_list(value.get("query_phrases", [])),
            "record_grain": str(value.get("record_grain") or "").strip(),
            "record_role": str(value.get("record_role") or "").strip(),
            "current_record": dict(value.get("current_record") or {}),
            "source": value.get("source", "USER_DEFINED"), "confidence": value.get("confidence", 1.0), "approval_status": value.get("approval_status", "USER_DEFINED"),
            "created_at": value.get("created_at"), "updated_at": value.get("updated_at"),
        })
        normalized["columns"] = {
            str(name): {
                "business_meaning": str((item or {}).get("business_meaning") or (item or {}).get("meaning") or "").strip(),
                "aliases": _clean_list((item or {}).get("aliases", [])),
                "synonyms": _clean_list((item or {}).get("synonyms", [])),
                "abbreviations": _clean_list((item or {}).get("abbreviations", [])),
                "important_values": dict((item or {}).get("important_values", {})),
                "common_phrases": _clean_list((item or {}).get("common_phrases", [])),
                "intended_usage": _clean_list((item or {}).get("intended_usage") or (item or {}).get("usage_notes") or []),
                "exclusions": _clean_list((item or {}).get("exclusions") or []),
            } for name, item in (value.get("columns") or {}).items()
        }
        normalized["predicate_mappings"] = [dict(item) for item in value.get("predicate_mappings", []) if isinstance(item, dict)]
        return normalized

    def _record(self, action: str, table: str, detail: Dict[str, Any]) -> None:
        self._data["history"].append({"at": datetime.now(timezone.utc).isoformat(), "action": action, "table": table, **detail})
        self._data["history"] = self._data["history"][-500:]

    def migrate(self, schema: Dict[str, Any], annotations: Dict[str, Dict[str, Any]], dictionary: Any) -> None:
        """Idempotently import old annotation and dictionary data without loss."""
        changed = False
        for table in schema.get("tables", []):
            name = table["name"]
            previous = self._data["tables"].get(name, {})
            entry = self._normalise_table(previous)
            self._data["tables"][name] = entry
            if previous != entry:
                changed = True
            annotation = annotations.get(name, {})
            if annotation.get("meaning") and not entry["definition"]:
                entry["definition"] = annotation["meaning"]
                changed = True
            merged_synonyms = _clean_list([*entry.get("synonyms", []), *annotation.get("aliases", [])])
            if merged_synonyms != entry.get("synonyms", []):
                entry["synonyms"] = merged_synonyms
                changed = True

        # Only migrate a legacy dictionary term when its ownership can be
        # determined from a table/column token. Ambiguous terms remain in the
        # explicit legacy bucket instead of being silently assigned wrongly.
        tokens_by_table = {}
        for table in schema.get("tables", []):
            values = [table["name"], *(column.get("name", "") for column in table.get("columns", []))]
            tokens_by_table[table["name"]] = {token.upper() for value in values for token in dictionary.tokenize(value)}
        for abbreviation, term in dictionary.get_all().items():
            owners = [name for name, tokens in tokens_by_table.items() if abbreviation.upper() in tokens]
            if len(owners) == 1:
                terms = self._data["tables"][owners[0]].setdefault("abbreviations", {})
                if abbreviation.upper() not in terms:
                    terms[abbreviation.upper()] = {"abbreviation": abbreviation.upper(), "meaning": term.get("expansion", ""), "column": ""}
                    changed = True
            elif abbreviation.upper() not in self._data["legacy_unassigned_terms"]:
                self._data["legacy_unassigned_terms"][abbreviation.upper()] = self._term_from_legacy(abbreviation, term)
                changed = True
        if changed:
            self._save()

    @staticmethod
    def _term_from_legacy(abbreviation: str, term: Dict[str, Any]) -> Dict[str, Any]:
        return {"abbreviation": abbreviation.upper(), "meaning": term.get("expansion", ""), "column": ""}

    def table(self, table_name: str) -> Dict[str, Any]:
        return self._normalise_table(self._data["tables"].get(table_name, {}))

    @staticmethod
    def is_defined(entry: Dict[str, Any]) -> bool:
        """True only for meaningful, explicitly user-authored knowledge."""
        return bool(
            str(entry.get("definition") or "").strip()
            or str(entry.get("business_entity") or "").strip()
            or str(entry.get("business_name") or "").strip()
            or entry.get("intended_usage")
            or entry.get("aliases")
            or entry.get("synonyms")
            or entry.get("common_phrases")
            or entry.get("abbreviations")
            or entry.get("columns")
            or entry.get("predicate_mappings")
            or entry.get("record_grain")
            or entry.get("record_role")
            or entry.get("current_record")
        )

    def is_table_defined(self, table_name: str) -> bool:
        return self.is_defined(self.table(table_name))

    def all(self) -> Dict[str, Dict[str, Any]]:
        return {name: self.table(name) for name in self._data["tables"]}

    def update_table(self, table_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if "record_role" in data and data["record_role"] not in {"", "entity", "snapshot", "event_history", "reference"}:
            raise ValueError("Invalid record role")
        if "current_record" in data:
            rule = data["current_record"]
            if rule not in (None, {}) and (not isinstance(rule, dict) or not str(rule.get("column") or "").strip() or rule.get("value") is None or isinstance(rule.get("value"), (dict, list))):
                raise ValueError("Current-record rule needs a column and a scalar value")
        entry = self._data["tables"].setdefault(table_name, self._empty_table())
        entry = self._normalise_table(entry)
        self._data["tables"][table_name] = entry
        if "definition" in data:
            entry["definition"] = str(data.get("definition") or "").strip()
        for field in ("record_grain", "record_role"):
            if field in data:
                entry[field] = str(data.get(field) or "").strip()
        if "current_record" in data:
            rule = data["current_record"] or {}
            entry["current_record"] = {"column": str(rule["column"]).strip(), "value": rule["value"]} if rule else {}
        for field in ("business_entity", "business_name"):
            if field in data:
                entry[field] = str(data.get(field) or "").strip()
        for field in ("aliases", "synonyms", "common_phrases", "query_phrases", "intended_usage", "exclusions"):
            if field in data:
                entry[field] = _clean_list(data[field])
        if any(field in data for field in ("business_entity", "business_name", "intended_usage", "exclusions", "aliases", "common_phrases", "query_phrases", "record_grain", "record_role", "current_record", "source", "confidence", "approval_status")):
            entry["source"] = str(data.get("source") or entry.get("source") or "USER_DEFINED")
            entry["confidence"] = float(data.get("confidence", entry.get("confidence", 1.0)) or 0)
            entry["approval_status"] = str(data.get("approval_status") or entry.get("approval_status") or "USER_DEFINED")
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            entry["created_at"] = entry.get("created_at") or entry["updated_at"]
        self._record("update_table", table_name, {"fields": sorted(data)})
        self._save()
        return self.table(table_name)

    def upsert_business_term(self, table_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        abbreviation = str(data.get("abbreviation") or "").strip().upper()
        if not abbreviation:
            raise ValueError("'abbreviation' is required")
        entry = self._data["tables"].setdefault(table_name, self._empty_table())
        entry = self._normalise_table(entry)
        self._data["tables"][table_name] = entry
        term = {"abbreviation": abbreviation, "meaning": str(data.get("meaning") or "").strip(), "column": str(data.get("column") or data.get("maps_to_column") or "").strip()}
        if str(data.get("context") or "").strip():
            term["context"] = str(data.get("context") or "").strip()
        if any(field in data for field in ("source", "confidence", "approval_status")):
            term.update({"source": str(data.get("source") or "USER_DEFINED"), "confidence": float(data.get("confidence", 1.0) or 0), "approval_status": str(data.get("approval_status") or "USER_DEFINED")})
        entry.setdefault("abbreviations", {})[abbreviation] = term
        self._record("upsert_business_term", table_name, {"abbreviation": abbreviation})
        self._save()
        return term

    def update_column(self, table_name: str, column_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        entry = self._data["tables"].setdefault(table_name, self._empty_table())
        entry = self._normalise_table(entry); self._data["tables"][table_name] = entry
        column = entry.setdefault("columns", {}).setdefault(column_name, {})
        column.update({
            "business_meaning": str(data.get("business_meaning") or data.get("meaning") or "").strip(),
            "aliases": _clean_list(data.get("aliases", [])), "synonyms": _clean_list(data.get("synonyms", [])),
            "abbreviations": _clean_list(data.get("abbreviations", [])), "important_values": dict(data.get("important_values", {})),
            "common_phrases": _clean_list(data.get("common_phrases", [])),
            "intended_usage": _clean_list(data.get("intended_usage") or data.get("usage_notes") or []),
            "exclusions": _clean_list(data.get("exclusions", [])),
            "source": str(data.get("source") or "USER_DEFINED"), "confidence": float(data.get("confidence", 1.0) or 0),
            "approval_status": str(data.get("approval_status") or "USER_DEFINED"),
        })
        self._record("update_column", table_name, {"column": column_name}); self._save()
        return dict(column)

    def upsert_predicate(self, table_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        phrase, column = str(data.get("phrase") or data.get("concept") or "").strip(), str(data.get("column") or "").strip()
        if not phrase or not column:
            raise ValueError("'phrase' and 'column' are required")
        item = {"phrase": phrase, "column": column, "operator": str(data.get("operator") or "="), "value": data.get("value"), "semantic_type": str(data.get("semantic_type") or "UNKNOWN"), "source": str(data.get("source") or "USER_DEFINED"), "confidence": float(data.get("confidence", 1.0) or 0), "approval_status": str(data.get("approval_status") or "USER_VERIFIED")}
        entry = self._data["tables"].setdefault(table_name, self._empty_table())
        entry = self._normalise_table(entry); self._data["tables"][table_name] = entry
        mappings = [m for m in entry.get("predicate_mappings", []) if m.get("phrase", "").lower() != phrase.lower()]
        mappings.append(item); entry["predicate_mappings"] = mappings
        self._record("upsert_predicate", table_name, {"phrase": phrase, "column": column}); self._save()
        return item

    def predicate_candidates(self, phrase: str, selected_tables: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        selected = set(selected_tables or [])
        candidates = []
        for table, entry in self.all().items():
            if selected and table not in selected:
                continue
            for mapping in entry.get("predicate_mappings", []):
                if mapping.get("approval_status") in {"USER_VERIFIED", "USER_DEFINED", "APPROVED"} and mapping.get("phrase", "").lower() == phrase.lower():
                    candidates.append({"table": table, **mapping})
        return sorted(candidates, key=lambda item: (-float(item.get("confidence", 0)), item["table"], item["column"]))

    def conflicts(self) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for table, entry in self.all().items():
            for mapping in entry.get("predicate_mappings", []):
                grouped.setdefault(mapping.get("phrase", "").lower(), []).append({"table": table, **mapping})
        return [{"phrase": phrase, "candidates": items} for phrase, items in grouped.items() if phrase and len({(i["table"], i.get("column"), str(i.get("value"))) for i in items}) > 1]

    def history(self, table_name: Optional[str] = None) -> List[Dict[str, Any]]:
        return [item for item in reversed(self._data["history"]) if not table_name or item.get("table") == table_name]

    def delete_business_term(self, table_name: str, abbreviation: str) -> bool:
        terms = self._data["tables"].get(table_name, {}).get("abbreviations", {})
        if abbreviation.upper() not in terms:
            return False
        del terms[abbreviation.upper()]
        self._record("delete_business_term", table_name, {"abbreviation": abbreviation.upper()})
        self._save()
        return True
