"""Deterministic schema-aware repair immediately before query safety checks.

The post-processor never creates SQL, changes joins, or changes business
conditions.  It only quotes reserved real identifiers and repairs an
unambiguous schema spelling/case error from already-loaded metadata.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.logger import get_logger

try:
    import sqlglot
    from sqlglot import exp
except ImportError:  # pragma: no cover
    sqlglot = None
    exp = None


logger = get_logger(__name__)

# MySQL reserved words that can legitimately occur as business table/column
# names.  This is SQL-dialect knowledge, not database-specific hardcoding.
MYSQL_RESERVED_IDENTIFIERS = {
    "ACCESSIBLE", "ADD", "ALL", "ALTER", "ANALYZE", "AND", "AS", "ASC",
    "BEFORE", "BETWEEN", "BIGINT", "BINARY", "BLOB", "BOTH", "BY", "CALL",
    "CASCADE", "CASE", "CHANGE", "CHAR", "CHECK", "COLUMN", "CONDITION",
    "CONSTRAINT", "CONTINUE", "CREATE", "CROSS", "CURRENT_DATE", "DATABASE",
    "DATABASES", "DAY_HOUR", "DEC", "DECIMAL", "DECLARE", "DEFAULT", "DELETE",
    "DESC", "DESCRIBE", "DISTINCT", "DOUBLE", "DROP", "EACH", "ELSE", "EXISTS",
    "FALSE", "FETCH", "FLOAT", "FOR", "FOREIGN", "FROM", "FULLTEXT", "GRANT",
    "GROUP", "GROUPS", "HAVING", "HIGH_PRIORITY", "HOUR_MINUTE", "IF", "IGNORE",
    "IN", "INDEX", "INNER", "INSERT", "INT", "INTEGER", "INTERVAL", "INTO",
    "IS", "ITERATE", "JOIN", "KEY", "KEYS", "KILL", "LEADING", "LEFT", "LIKE",
    "LIMIT", "LINEAR", "LINES", "LOAD", "LOCALTIME", "LOCK", "LONG", "LOOP",
    "MASTER", "MATCH", "MEDIUMINT", "MOD", "MODIFIES", "NATURAL", "NOT", "NULL",
    "NUMERIC", "ON", "OPTION", "OPTIONALLY", "OR", "ORDER", "OUT", "OUTER",
    "PRIMARY", "PROCEDURE", "PURGE", "RANGE", "READ", "REAL", "REFERENCES",
    "REGEXP", "RELEASE", "RENAME", "REPEAT", "REPLACE", "REQUIRE", "RESTRICT",
    "RETURN", "REVOKE", "RIGHT", "RLIKE", "SCHEMA", "SCHEMAS", "SELECT", "SET",
    "SHOW", "SMALLINT", "SPATIAL", "SQL", "SQLSTATE", "SSL", "TABLE", "TERMINATED",
    "THEN", "TINYINT", "TO", "TRAILING", "TRIGGER", "TRUE", "UNDO", "UNION",
    "UNIQUE", "UNLOCK", "UNSIGNED", "UPDATE", "USAGE", "USE", "USING", "UTC_DATE",
    "VALUES", "VARCHAR", "VARYING", "WHEN", "WHERE", "WHILE", "WITH", "WRITE",
    "XOR", "YEAR_MONTH", "ZEROFILL", "USER",
}


class SQLPostProcessingError(ValueError):
    """SQL references schema objects that cannot be repaired with certainty."""


@dataclass
class SchemaValidationResult:
    valid: bool
    missing_tables: List[str] = field(default_factory=list)
    missing_columns: List[str] = field(default_factory=list)
    invalid_aliases: List[str] = field(default_factory=list)
    ambiguous_columns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    """Small, dependency-free Levenshtein check used only for exact repairs."""
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    if len(left) > len(right):
        left, right = right, left
    index = mismatch = 0
    while index < len(left):
        if left[index] == right[index + mismatch]:
            index += 1
        else:
            mismatch += 1
            if mismatch > 1:
                return False
    return True


class IdentifierNormalizer:
    """Quote reserved schema identifiers and correct only their case."""

    def __init__(self, schema: Dict[str, Any]):
        self.tables = {table["name"].lower(): table for table in schema.get("tables", [])}

    @staticmethod
    def _quote(identifier: Any) -> None:
        identifier.set("quoted", True)

    def normalize(self, sql: str) -> str:
        if sqlglot is None:
            raise SQLPostProcessingError("SQL parser dependency is unavailable.")
        try:
            tree = sqlglot.parse_one(sql, read="mysql")
        except Exception as exc:
            raise SQLPostProcessingError(f"Generated SQL could not be parsed: {exc}") from exc
        aliases: Dict[str, Dict[str, Any]] = {}
        for table_expression in tree.find_all(exp.Table):
            table = self.tables.get(table_expression.name.lower())
            if table is None:
                continue
            actual_name = table["name"]
            table_expression.this.set("this", actual_name)
            if actual_name.upper() in MYSQL_RESERVED_IDENTIFIERS:
                self._quote(table_expression.this)
                logger.info("Identifier normalizer escaped table: %s", actual_name)
            aliases[(table_expression.alias or actual_name).lower()] = table

        for column in tree.find_all(exp.Column):
            qualifier = (column.table or "").lower()
            table = aliases.get(qualifier) if qualifier else None
            if table is None:
                continue
            actual_columns = {item["name"].lower(): item["name"] for item in table.get("columns", [])}
            actual_name = actual_columns.get(column.name.lower())
            if actual_name is None:
                continue
            column.this.set("this", actual_name)
            if column.args.get("table") and qualifier == table["name"].lower():
                column.args["table"].set("this", table["name"])
                if table["name"].upper() in MYSQL_RESERVED_IDENTIFIERS:
                    self._quote(column.args["table"])
            if actual_name.upper() in MYSQL_RESERVED_IDENTIFIERS:
                self._quote(column.this)
                logger.info("Identifier normalizer escaped column: %s.%s", table["name"], actual_name)
        return tree.sql(dialect="mysql")


class SchemaReferenceValidator:
    """Validate table, alias, and column references against cached schema."""

    def __init__(self, schema: Dict[str, Any]):
        self.tables = {table["name"].lower(): table for table in schema.get("tables", [])}

    def _aliases(self, tree: Any, result: SchemaValidationResult) -> Dict[str, Dict[str, Any]]:
        aliases: Dict[str, Dict[str, Any]] = {}
        for table_expression in tree.find_all(exp.Table):
            name = table_expression.name
            table = self.tables.get(name.lower())
            if table is None:
                result.missing_tables.append(name)
                continue
            aliases[(table_expression.alias or table["name"]).lower()] = table
        return aliases

    def validate(self, sql: str) -> SchemaValidationResult:
        if sqlglot is None:
            return SchemaValidationResult(False, missing_tables=["<sqlglot unavailable>"])
        try:
            tree = sqlglot.parse_one(sql, read="mysql")
        except Exception as exc:
            return SchemaValidationResult(False, missing_tables=[f"<parse error: {exc}>"])
        result = SchemaValidationResult(valid=True)
        aliases = self._aliases(tree, result)
        referenced_tables = list({table["name"].lower(): table for table in aliases.values()}.values())
        for column in tree.find_all(exp.Column):
            qualifier = (column.table or "").lower()
            if qualifier:
                table = aliases.get(qualifier)
                if table is None:
                    result.invalid_aliases.append(column.table)
                    continue
                if column.name.lower() not in {item["name"].lower() for item in table.get("columns", [])}:
                    result.missing_columns.append(f"{table['name']}.{column.name}")
                continue
            candidates = [
                table for table in referenced_tables
                if column.name.lower() in {item["name"].lower() for item in table.get("columns", [])}
            ]
            if not candidates:
                result.missing_columns.append(column.name)
            elif len(candidates) > 1:
                result.ambiguous_columns.append(column.name)
        for key in ("missing_tables", "missing_columns", "invalid_aliases", "ambiguous_columns"):
            setattr(result, key, list(dict.fromkeys(getattr(result, key))))
        result.valid = not any((result.missing_tables, result.missing_columns, result.invalid_aliases, result.ambiguous_columns))
        return result


class SchemaAwareAutoCorrector:
    """Repair only one unambiguous case, dictionary, or edit-distance error."""

    def __init__(self, schema: Dict[str, Any], dictionary: Any = None):
        self.tables = {table["name"].lower(): table for table in schema.get("tables", [])}
        self.dictionary = dictionary

    def _dictionary_equivalent(self, source: str, candidate: str) -> bool:
        if self.dictionary is None:
            return False
        try:
            expansion = self.dictionary.expand_token(source)
            return bool(expansion and expansion.strip().lower() == candidate.lower())
        except Exception:
            return False

    def correct(self, sql: str) -> str:
        if sqlglot is None:
            return sql
        tree = sqlglot.parse_one(sql, read="mysql")
        aliases: Dict[str, Dict[str, Any]] = {}
        for table_expression in tree.find_all(exp.Table):
            table = self.tables.get(table_expression.name.lower())
            if table is not None:
                aliases[(table_expression.alias or table["name"]).lower()] = table

        for column in tree.find_all(exp.Column):
            qualifier = (column.table or "").lower()
            table = aliases.get(qualifier)
            if table is None:
                continue
            current = column.name
            candidates = []
            for schema_column in table.get("columns", []):
                candidate = schema_column["name"]
                if _edit_distance_at_most_one(current.lower(), candidate.lower()) or self._dictionary_equivalent(current, candidate):
                    candidates.append(candidate)
            candidates = list(dict.fromkeys(candidates))
            if len(candidates) == 1 and candidates[0].lower() != current.lower():
                column.this.set("this", candidates[0])
                logger.info("Schema auto-corrector corrected %s.%s -> %s (100%% confidence)", table["name"], current, candidates[0])
        return tree.sql(dialect="mysql")


class SQLPostProcessor:
    """Compose normalization, schema validation, and certain corrections."""

    def __init__(self, schema: Dict[str, Any], dictionary: Any = None):
        self.normalizer = IdentifierNormalizer(schema)
        self.validator = SchemaReferenceValidator(schema)
        self.corrector = SchemaAwareAutoCorrector(schema, dictionary)

    def process(self, sql: str) -> str:
        normalized = self.normalizer.normalize(sql)
        result = self.validator.validate(normalized)
        if not result.valid:
            logger.warning("Schema validator found invalid SQL references: %s", result.to_dict())
            normalized = self.normalizer.normalize(self.corrector.correct(normalized))
            result = self.validator.validate(normalized)
        if not result.valid:
            raise SQLPostProcessingError(f"Schema validation failed: {result.to_dict()}")
        logger.info("SQL post-processing validation passed; query is ready for Query Guard.")
        return normalized
