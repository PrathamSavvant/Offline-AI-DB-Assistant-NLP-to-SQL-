"""
core/scanner.py

Phase 1 — Database Scanner

Responsible for extracting ALL structural metadata from a MySQL database
using INFORMATION_SCHEMA exclusively. No table names are hardcoded, so this
works against any MySQL database supplied via configuration.

This module does NOT export anything to disk — that responsibility belongs
to core/exporter.py. The scanner's only job is to query the database and
return clean, well-structured Python data (dicts/lists) describing the
database.
"""

from collections import defaultdict
from typing import Any, Dict, List, Optional

from core.database import Database
from core.logger import get_logger

logger = get_logger(__name__)


class DatabaseScanner:
    """
    Extracts schema-level metadata from a MySQL database via
    INFORMATION_SCHEMA queries.

    Usage:
        db = Database()
        db.connect()

        scanner = DatabaseScanner(db, database_name="my_database")
        schema = scanner.scan_database()
    """

    def __init__(self, db: Database, database_name: str):
        self.db = db
        self.database_name = database_name

    # ------------------------------------------------------------------
    # TABLES
    # ------------------------------------------------------------------

    def get_tables(self) -> List[Dict[str, Any]]:
        """
        Return basic metadata for every base table and view in the database.
        """

        logger.info("Fetching table list for database '%s'", self.database_name)

        query = """
            SELECT
                TABLE_NAME,
                TABLE_TYPE,
                ENGINE,
                TABLE_ROWS,
                CREATE_TIME,
                UPDATE_TIME,
                TABLE_COMMENT
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
        """

        rows = self.db.execute(query, (self.database_name,))

        tables = []
        for row in rows:
            tables.append({
                "name": row["TABLE_NAME"],
                "type": row["TABLE_TYPE"],  # 'BASE TABLE' or 'VIEW'
                "engine": row["ENGINE"],
                "approx_row_count": row["TABLE_ROWS"],
                "created_at": self._stringify(row["CREATE_TIME"]),
                "updated_at": self._stringify(row["UPDATE_TIME"]),
                "comment": row["TABLE_COMMENT"] or None,
            })

        logger.info("Found %d tables/views", len(tables))
        return tables

    # ------------------------------------------------------------------
    # COLUMNS
    # ------------------------------------------------------------------

    def get_columns(self, table_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return column metadata for a single table, or for all tables if
        table_name is None.
        """

        logger.info(
            "Fetching columns for %s",
            f"table '{table_name}'" if table_name else "all tables",
        )

        query = """
            SELECT
                TABLE_NAME,
                COLUMN_NAME,
                ORDINAL_POSITION,
                COLUMN_DEFAULT,
                IS_NULLABLE,
                DATA_TYPE,
                CHARACTER_MAXIMUM_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE,
                COLUMN_TYPE,
                COLUMN_KEY,
                EXTRA,
                COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
        """
        params: List[Any] = [self.database_name]

        if table_name:
            query += " AND TABLE_NAME = %s"
            params.append(table_name)

        query += " ORDER BY TABLE_NAME, ORDINAL_POSITION"

        rows = self.db.execute(query, tuple(params))

        columns = []
        for row in rows:
            columns.append({
                "table": row["TABLE_NAME"],
                "name": row["COLUMN_NAME"],
                "position": row["ORDINAL_POSITION"],
                "default": row["COLUMN_DEFAULT"],
                "nullable": row["IS_NULLABLE"] == "YES",
                "datatype": row["DATA_TYPE"],
                "full_type": row["COLUMN_TYPE"],
                "max_length": row["CHARACTER_MAXIMUM_LENGTH"],
                "numeric_precision": row["NUMERIC_PRECISION"],
                "numeric_scale": row["NUMERIC_SCALE"],
                "is_primary_key": row["COLUMN_KEY"] == "PRI",
                "is_unique": row["COLUMN_KEY"] == "UNI",
                "auto_increment": "auto_increment" in (row["EXTRA"] or ""),
                "comment": row["COLUMN_COMMENT"] or None,
            })

        return columns

    # ------------------------------------------------------------------
    # PRIMARY KEYS
    # ------------------------------------------------------------------

    def get_primary_keys(self, table_name: Optional[str] = None) -> Dict[str, List[str]]:
        """
        Return a mapping of table_name -> ordered list of primary key
        column names. Handles composite primary keys correctly.
        """

        logger.info("Fetching primary keys")

        query = """
            SELECT
                TABLE_NAME,
                COLUMN_NAME,
                ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND CONSTRAINT_NAME = 'PRIMARY'
        """
        params: List[Any] = [self.database_name]

        if table_name:
            query += " AND TABLE_NAME = %s"
            params.append(table_name)

        query += " ORDER BY TABLE_NAME, ORDINAL_POSITION"

        rows = self.db.execute(query, tuple(params))

        primary_keys: Dict[str, List[str]] = defaultdict(list)
        for row in rows:
            primary_keys[row["TABLE_NAME"]].append(row["COLUMN_NAME"])

        return dict(primary_keys)

    # ------------------------------------------------------------------
    # FOREIGN KEYS
    # ------------------------------------------------------------------

    def get_foreign_keys(self, table_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return every foreign key relationship in the database (or for a
        single table), including the constraint name and the referenced
        table/column.
        """

        logger.info("Fetching foreign keys")

        query = """
            SELECT
                TABLE_NAME,
                COLUMN_NAME,
                CONSTRAINT_NAME,
                REFERENCED_TABLE_NAME,
                REFERENCED_COLUMN_NAME,
                ORDINAL_POSITION,
                POSITION_IN_UNIQUE_CONSTRAINT
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """
        params: List[Any] = [self.database_name]

        if table_name:
            query += " AND TABLE_NAME = %s"
            params.append(table_name)

        query += " ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"

        rows = self.db.execute(query, tuple(params))

        foreign_keys = []
        for row in rows:
            foreign_keys.append({
                "table": row["TABLE_NAME"],
                "column": row["COLUMN_NAME"],
                "constraint_name": row["CONSTRAINT_NAME"],
                "references_table": row["REFERENCED_TABLE_NAME"],
                "references_column": row["REFERENCED_COLUMN_NAME"],
                "ordinal_position": row.get("ORDINAL_POSITION"),
                "referenced_ordinal_position": row.get("POSITION_IN_UNIQUE_CONSTRAINT"),
            })

        return foreign_keys

    # ------------------------------------------------------------------
    # INDEXES
    # ------------------------------------------------------------------

    def get_indexes(self, table_name: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Return a mapping of table_name -> list of indexes, each with its
        name, uniqueness, and ordered list of columns.
        """

        logger.info("Fetching indexes")

        query = """
            SELECT
                TABLE_NAME,
                INDEX_NAME,
                COLUMN_NAME,
                NON_UNIQUE,
                SEQ_IN_INDEX
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s
        """
        params: List[Any] = [self.database_name]

        if table_name:
            query += " AND TABLE_NAME = %s"
            params.append(table_name)

        query += " ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"

        rows = self.db.execute(query, tuple(params))

        # Group rows into: table -> index_name -> {unique, columns: []}
        grouped: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

        for row in rows:
            table = row["TABLE_NAME"]
            index_name = row["INDEX_NAME"]

            if index_name not in grouped[table]:
                grouped[table][index_name] = {
                    "name": index_name,
                    "unique": row["NON_UNIQUE"] == 0,
                    "columns": [],
                }

            grouped[table][index_name]["columns"].append(row["COLUMN_NAME"])

        indexes: Dict[str, List[Dict[str, Any]]] = {}
        for table, idx_map in grouped.items():
            indexes[table] = list(idx_map.values())

        return indexes

    # ------------------------------------------------------------------
    # VIEWS
    # ------------------------------------------------------------------

    def get_views(self) -> List[Dict[str, Any]]:
        """
        Return every view in the database along with its SELECT definition.
        """

        logger.info("Fetching views")

        query = """
            SELECT
                TABLE_NAME,
                VIEW_DEFINITION,
                CHECK_OPTION,
                IS_UPDATABLE
            FROM INFORMATION_SCHEMA.VIEWS
            WHERE TABLE_SCHEMA = %s
            ORDER BY TABLE_NAME
        """

        rows = self.db.execute(query, (self.database_name,))

        views = []
        for row in rows:
            views.append({
                "name": row["TABLE_NAME"],
                "definition": row["VIEW_DEFINITION"],
                "check_option": row["CHECK_OPTION"],
                "is_updatable": row["IS_UPDATABLE"] == "YES",
            })

        return views

    # ------------------------------------------------------------------
    # TRIGGERS
    # ------------------------------------------------------------------

    def get_triggers(self) -> List[Dict[str, Any]]:
        """
        Return every trigger defined in the database.
        """

        logger.info("Fetching triggers")

        query = """
            SELECT
                TRIGGER_NAME,
                EVENT_MANIPULATION,
                EVENT_OBJECT_TABLE,
                ACTION_TIMING,
                ACTION_STATEMENT
            FROM INFORMATION_SCHEMA.TRIGGERS
            WHERE TRIGGER_SCHEMA = %s
            ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME
        """

        rows = self.db.execute(query, (self.database_name,))

        triggers = []
        for row in rows:
            triggers.append({
                "name": row["TRIGGER_NAME"],
                "event": row["EVENT_MANIPULATION"],       # INSERT / UPDATE / DELETE
                "table": row["EVENT_OBJECT_TABLE"],
                "timing": row["ACTION_TIMING"],            # BEFORE / AFTER
                "statement": row["ACTION_STATEMENT"],
            })

        return triggers

    # ------------------------------------------------------------------
    # STORED PROCEDURES / FUNCTIONS
    # ------------------------------------------------------------------

    def get_procedures(self) -> List[Dict[str, Any]]:
        """
        Return every stored procedure and function in the database.
        Note: ROUTINE_DEFINITION can be NULL if the user lacks privileges
        to view the routine body.
        """

        logger.info("Fetching stored procedures/functions")

        query = """
            SELECT
                ROUTINE_NAME,
                ROUTINE_TYPE,
                ROUTINE_DEFINITION,
                CREATED,
                LAST_ALTERED
            FROM INFORMATION_SCHEMA.ROUTINES
            WHERE ROUTINE_SCHEMA = %s
            ORDER BY ROUTINE_NAME
        """

        rows = self.db.execute(query, (self.database_name,))

        procedures = []
        for row in rows:
            procedures.append({
                "name": row["ROUTINE_NAME"],
                "type": row["ROUTINE_TYPE"],  # PROCEDURE or FUNCTION
                "definition": row["ROUTINE_DEFINITION"],
                "created_at": self._stringify(row["CREATED"]),
                "last_altered": self._stringify(row["LAST_ALTERED"]),
            })

        return procedures

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def scan_database(self) -> Dict[str, Any]:
        """
        Run the full Phase 1 scan and assemble a single, complete
        structure describing the database:

            {
                "database": str,
                "tables": [
                    {
                        "name": str,
                        "type": str,
                        "engine": str,
                        "approx_row_count": int,
                        "comment": str | None,
                        "columns": [ {...} ],
                        "primary_key": [str],
                        "foreign_keys": [ {...} ],
                        "indexes": [ {...} ],
                    },
                    ...
                ],
                "views": [ {...} ],
                "triggers": [ {...} ],
                "procedures": [ {...} ],
            }

        This is the single source of truth handed to core/exporter.py.
        """

        logger.info("Starting full database scan for '%s'", self.database_name)

        tables = self.get_tables()
        all_columns = self.get_columns()
        primary_keys = self.get_primary_keys()
        all_foreign_keys = self.get_foreign_keys()
        all_indexes = self.get_indexes()

        # Group columns by table for O(1) lookup while assembling.
        columns_by_table: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for col in all_columns:
            columns_by_table[col["table"]].append(col)

        # Group foreign keys by table.
        fks_by_table: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for fk in all_foreign_keys:
            fks_by_table[fk["table"]].append(fk)

        assembled_tables = []
        for table in tables:
            table_name = table["name"]

            assembled_tables.append({
                **table,
                "columns": columns_by_table.get(table_name, []),
                "primary_key": primary_keys.get(table_name, []),
                "foreign_keys": fks_by_table.get(table_name, []),
                "indexes": all_indexes.get(table_name, []),
            })

        schema = {
            "database": self.database_name,
            "tables": assembled_tables,
            "views": self.get_views(),
            "triggers": self.get_triggers(),
            "procedures": self.get_procedures(),
        }

        logger.info(
            "Scan complete: %d tables, %d views, %d triggers, %d procedures",
            len(assembled_tables),
            len(schema["views"]),
            len(schema["triggers"]),
            len(schema["procedures"]),
        )

        return schema

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    @staticmethod
    def _stringify(value: Any) -> Optional[str]:
        """
        Convert datetime-like values from the connector into ISO-formatted
        strings so the final structure is always JSON-serializable.
        """

        if value is None:
            return None
        return str(value)

    # ------------------------------------------------------------------
    # CRAWLER ORCHESTRATION (extension)
    # ------------------------------------------------------------------
    #
    # scan_database() above is unchanged and keeps working exactly as it
    # always has — every existing caller (main.py's Phase 1) is unaffected.
    #
    # crawl() is additive: it wraps scan_database() with the new "Schema
    # Crawler" responsibilities requested for this project — automatic
    # discovery needs no manual metadata updates (scan_database() already
    # re-derives everything from INFORMATION_SCHEMA on every call), plus
    # schema-change detection and unknown-abbreviation detection against
    # the Business Dictionary.

    def crawl(self) -> Dict[str, Any]:
        """
        Run a full crawl: scan the database, diff the result against the
        previous crawl's schema.json, and flag any abbreviation-shaped
        token in table/column names that isn't yet in the Business
        Dictionary.

        Returns:
            {
                "schema": <same structure as scan_database()>,
                "schema_diff": {...},           # core/schema_differ.py
                "new_pending_abbreviations": [...],
            }
        """

        # Local imports to avoid a hard dependency for callers that only
        # need scan_database() (e.g. tests, or a lighter-weight embedding).
        from core.business_dictionary import business_dictionary
        from core.pending_dictionary import pending_dictionary
        from core.schema_differ import SchemaDiffer

        logger.info("Starting crawl for '%s'", self.database_name)

        differ = SchemaDiffer()
        previous_schema = differ.load_previous_schema()

        schema = self.scan_database()

        diff = differ.compare(previous_schema, schema)
        differ.persist(diff)

        new_pending = pending_dictionary.scan_schema(schema, business_dictionary)

        logger.info(
            "Crawl complete: %d table(s), %d schema change(s), %d new unknown abbreviation(s)",
            len(schema["tables"]),
            len(diff["added_tables"]) + len(diff["removed_tables"])
            + len(diff["added_columns"]) + len(diff["removed_columns"]),
            len(new_pending),
        )

        return {
            "schema": schema,
            "schema_diff": diff,
            "new_pending_abbreviations": new_pending,
        }
