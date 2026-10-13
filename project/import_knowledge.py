"""Validate and atomically import DBA-approved meanings for the active database.

Dry-run is the default. Add --apply only after reviewing the input and the
printed summary. Restart the application after applying an external import.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core.db_context import db_context
from core.defined_knowledge import DefinedKnowledgeRepository
from core.knowledge_readiness import inspect_cached_artifacts

TABLE_FIELDS = frozenset({"definition", "business_entity", "business_name", "aliases", "synonyms",
                          "common_phrases", "query_phrases", "intended_usage", "exclusions",
                          "record_grain", "record_role", "current_record"})
LIST_FIELDS = frozenset({"aliases", "synonyms", "common_phrases", "query_phrases", "intended_usage", "exclusions"})
COLUMN_FIELDS = frozenset({"business_meaning", "aliases", "synonyms", "abbreviations", "important_values",
                           "common_phrases", "intended_usage", "exclusions"})


def validate_document(document: dict, schema: dict, database_id: str, fingerprint: str) -> list[str]:
    """Validate the whole package before any persistent write."""
    errors = []
    if document.get("database_id") != database_id:
        errors.append("database_id does not match the active database")
    if document.get("schema_fingerprint") != fingerprint:
        errors.append("schema_fingerprint does not match the active schema")
    tables = document.get("tables")
    if not isinstance(tables, dict) or not tables:
        return errors + ["tables must be a non-empty object"]
    schema_tables = {table["name"]: {column["name"] for column in table.get("columns", [])}
                     for table in schema.get("tables", [])}
    predicate_owners = {}
    for table_name, data in tables.items():
        prefix = f"tables.{table_name}"
        if table_name not in schema_tables:
            errors.append(f"{prefix}: unknown table")
            continue
        if not isinstance(data, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        for field in set(data) - TABLE_FIELDS - {"columns", "abbreviations", "predicate_mappings"}:
            errors.append(f"{prefix}.{field}: unsupported field")
        for field in LIST_FIELDS & set(data):
            if not isinstance(data[field], list) or not all(isinstance(x, str) and x.strip() for x in data[field]):
                errors.append(f"{prefix}.{field}: must be a list of non-empty strings")
        for field in (TABLE_FIELDS - LIST_FIELDS) & set(data):
            if field == "current_record":
                continue
            if not isinstance(data[field], str):
                errors.append(f"{prefix}.{field}: must be a string")
            elif len(data[field]) > 2000:
                errors.append(f"{prefix}.{field}: exceeds 2000 characters")
        if data.get("record_role") and data["record_role"] not in {"entity", "snapshot", "event_history", "reference"}:
            errors.append(f"{prefix}.record_role: unsupported role")
        rule = data.get("current_record")
        if rule is not None and rule != {}:
            if not isinstance(rule, dict) or rule.get("column") not in schema_tables[table_name] or not isinstance(rule.get("value"), (str, int, float, bool)):
                errors.append(f"{prefix}.current_record: requires an existing column and scalar value")
        columns = data.get("columns", {})
        if not isinstance(columns, dict):
            errors.append(f"{prefix}.columns: must be an object")
        else:
            for column_name, column_data in columns.items():
                if column_name not in schema_tables[table_name] or not isinstance(column_data, dict):
                    errors.append(f"{prefix}.columns.{column_name}: unknown column or invalid object")
                    continue
                for field in set(column_data) - COLUMN_FIELDS:
                    errors.append(f"{prefix}.columns.{column_name}.{field}: unsupported field")
                if "business_meaning" in column_data and not isinstance(column_data["business_meaning"], str):
                    errors.append(f"{prefix}.columns.{column_name}.business_meaning: must be a string")
                for field in (LIST_FIELDS | {"abbreviations"}) & set(column_data):
                    if not isinstance(column_data[field], list) or not all(isinstance(x, str) and x.strip() for x in column_data[field]):
                        errors.append(f"{prefix}.columns.{column_name}.{field}: must be a list of non-empty strings")
                if "important_values" in column_data and not isinstance(column_data["important_values"], dict):
                    errors.append(f"{prefix}.columns.{column_name}.important_values: must be an object")
        abbreviations = data.get("abbreviations", {})
        if not isinstance(abbreviations, dict):
            errors.append(f"{prefix}.abbreviations: must be an object")
        else:
            for token, item in abbreviations.items():
                if (not isinstance(token, str) or not token.strip() or not isinstance(item, dict)
                        or not isinstance(item.get("meaning"), str) or not item["meaning"].strip()):
                    errors.append(f"{prefix}.abbreviations.{token}: needs a non-empty meaning")
                else:
                    if set(item) - {"meaning", "column", "context"}:
                        errors.append(f"{prefix}.abbreviations.{token}: unsupported field")
                    if item.get("column") and item["column"] not in schema_tables[table_name]:
                        errors.append(f"{prefix}.abbreviations.{token}: unknown column")
                    if len(str(item["meaning"])) > 500:
                        errors.append(f"{prefix}.abbreviations.{token}: meaning exceeds 500 characters")
        mappings = data.get("predicate_mappings", [])
        if not isinstance(mappings, list):
            errors.append(f"{prefix}.predicate_mappings: must be a list")
        else:
            for index, item in enumerate(mappings):
                if (not isinstance(item, dict) or not isinstance(item.get("phrase"), str) or not item["phrase"].strip()
                        or item.get("column") not in schema_tables[table_name]
                        or item.get("operator", "=") != "="
                        or not isinstance(item.get("value"), (str, int, float, bool))):
                    errors.append(f"{prefix}.predicate_mappings[{index}]: requires phrase, existing column, scalar value, and '=' operator")
                else:
                    if set(item) - {"phrase", "column", "operator", "value"}:
                        errors.append(f"{prefix}.predicate_mappings[{index}]: unsupported field")
                    phrase = item["phrase"].strip().casefold()
                    target = (table_name, item["column"], str(item["value"]))
                    if phrase in predicate_owners and predicate_owners[phrase] != target:
                        errors.append(f"{prefix}.predicate_mappings[{index}]: phrase conflicts with another approved mapping")
                    predicate_owners[phrase] = target
    return errors


def apply_document(document: dict, target: Path) -> None:
    """Stage all edits and atomically replace only the approved-knowledge file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="knowledge-import-", dir=target.parent) as temporary_dir:
        staged = Path(temporary_dir) / "defined_knowledge.json"
        if target.exists():
            shutil.copy2(target, staged)
        repository = DefinedKnowledgeRepository(staged)
        existing_conflicts = {item["phrase"] for item in repository.conflicts()}
        for table, data in document["tables"].items():
            base = {key: value for key, value in data.items() if key in TABLE_FIELDS}
            if base:
                repository.update_table(table, base)
            for column, incoming in data.get("columns", {}).items():
                previous = repository.table(table).get("columns", {}).get(column, {})
                repository.update_column(table, column, {**previous, **incoming})
            for token, item in data.get("abbreviations", {}).items():
                repository.upsert_business_term(table, {"abbreviation": token, **{
                    key: item[key] for key in ("meaning", "column", "context") if key in item
                }})
            for item in data.get("predicate_mappings", []):
                repository.upsert_predicate(table, {key: item[key] for key in ("phrase", "column", "operator", "value") if key in item})
        new_conflicts = {item["phrase"] for item in repository.conflicts()} - existing_conflicts
        if new_conflicts:
            raise ValueError("Import would introduce conflicting predicate phrases: " + ", ".join(sorted(new_conflicts)))
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(target, target.with_name(f"defined_knowledge.backup-{stamp}.json"))
        staged.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="DBA-reviewed JSON package")
    parser.add_argument("--apply", action="store_true", help="Commit the validated package")
    args = parser.parse_args()
    directory = db_context.get_knowledge_dir()
    artifacts = inspect_cached_artifacts(directory)
    if not artifacts["valid"]:
        print("Active schema cache is incomplete: " + "; ".join(artifacts["issues"]), file=sys.stderr)
        return 2
    try:
        document = json.loads(args.file.read_text(encoding="utf-8"))
        schema = json.loads((directory / "schema.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Cannot read knowledge package or cached schema: {exc}", file=sys.stderr)
        return 2
    if not isinstance(document, dict):
        print("Knowledge package must be a JSON object.", file=sys.stderr)
        return 2
    fingerprint = artifacts["fingerprint"]
    errors = validate_document(document, schema, db_context.database_id, fingerprint)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 2
    count = len(document["tables"])
    if not args.apply:
        print(f"VALIDATED {count} table(s) for {db_context.active_database}; no changes made. Add --apply to import.")
        return 0
    try:
        apply_document(document, directory / "defined_knowledge.json")
    except (OSError, ValueError) as exc:
        print(f"Import was not applied: {exc}", file=sys.stderr)
        return 2
    print(f"Imported {count} approved table(s). Restart the application to refresh in-memory retrieval and prompts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
