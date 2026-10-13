"""
core/schema_cache.py

Startup schema cache. Reads schema, relationships, groups, semantics, and
business dictionary ONCE at application startup and holds everything in
memory for the lifetime of the process.

Never scans the database again unless the user explicitly calls refresh().
All schema questions hit the cache; the database is never queried for
metadata during normal operation.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_context import db_context
from core.logger import get_logger
from core.inverted_index import InvertedTableIndex
from core.knowledge_files import RUNTIME_CACHE_FILES
from core.table_knowledge import TableKnowledgeBuilder, TableKnowledgeRepository
from core.semantic_relationships import SemanticRelationshipRepository
from core.relationship_builder import RelationshipBuilder
from core import schema_fingerprint

try:
    from core.defined_knowledge import DefinedKnowledgeRepository as _DKRepo
except Exception:  # pragma: no cover
    _DKRepo = None

logger = get_logger(__name__)


class SchemaCache:
    """
    In-memory cache of the full knowledge base.
    Populated once at startup by load().
    """

    def __init__(self):
        self._loaded = False
        self.schema: Dict[str, Any] = {}
        self.fingerprint: Optional[str] = None
        self.tables: List[Dict[str, Any]] = []
        self.tables_by_name: Dict[str, Dict[str, Any]] = {}
        self.grouping: Dict[str, Any] = {}
        self.semantics: Dict[str, Any] = {}
        self.ranking: Dict[str, Any] = {}
        self.relationships: Dict[str, Any] = {}
        self.joins: Dict[str, Any] = {}
        self.table_profiles: Dict[str, Dict[str, Any]] = {}
        self.semantic_relationships: List[Dict[str, Any]] = []
        self.inverted_index = InvertedTableIndex()
        # Phase 17: FK-only graph and diagnostics
        self.fk_graph: Dict[str, Any] = {}
        self._fk_diagnostics: Dict[str, Any] = {}

    @property
    def knowledge_dir(self) -> Path:
        return db_context.get_knowledge_dir()

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        Load all knowledge-base JSON files into memory for the active database.
        Call once at Flask startup or upon database switch.
        """
        for name in RUNTIME_CACHE_FILES:
            self._require_file(name)

        self.schema = self._load_json("schema.json")
        self.fingerprint = schema_fingerprint.compute(self.schema)
        self.tables = self.schema.get("tables", [])
        self.tables_by_name = {t["name"]: t for t in self.tables}
        semantic_repository = SemanticRelationshipRepository(
            self.knowledge_dir / "semantic_relationships.json"
        )
        semantic_repository.validate_against_schema(self.schema)
        self.semantic_relationships = semantic_repository.approved()

        self.grouping = self._load_json("table_groups.json")
        self.semantics = self._load_json("business_semantics.json")
        self.ranking = self._load_json("table_importance.json")

        # Optional files — graceful fallback
        self.relationships = self._load_json_optional("relationships.json", {"relationships": []})
        self.joins = self._load_json_optional("joins.json", {})
        repository = TableKnowledgeRepository(self.knowledge_dir / "tables")
        self.table_profiles = repository.load_all()
        # repository.load_all() automatically migrates v1→v2 profiles in-place.
        # If the profile count still mismatches after loading (e.g. brand-new DB
        # that has never had a full profile build), generate profiles on-the-fly
        # so the runtime always has a profile for every table.
        if len(self.table_profiles) != len(self.tables):
            logger.info(
                "SchemaCache: %d table(s) have no profile — building on-the-fly (run main.py for a full build)",
                len(self.tables) - len(self.table_profiles),
            )
            self.table_profiles = TableKnowledgeBuilder(
                self.schema, self.semantics, self.relationships, self.grouping, self.ranking,
            ).build_all(self.table_profiles)
            repository.save_all(self.table_profiles)

        # Pass defined knowledge into the inverted index so table synonyms
        # and abbreviations from DefinedKnowledgeRepository are indexed at
        # startup.  The repository's own storage and write paths are never
        # touched here — we only read from its data.
        defined_knowledge_data: dict = {}
        if _DKRepo is not None:
            try:
                dk_repo = _DKRepo(self.knowledge_dir / "defined_knowledge.json")
                defined_knowledge_data = dk_repo.all()
            except Exception as _dk_exc:
                logger.debug("Could not load defined_knowledge for index enrichment: %s", _dk_exc)

        self.inverted_index.build(
            self.schema,
            grouping_data=self.grouping,
            semantics_data=self.semantics,
            table_profiles=self.table_profiles,
            defined_knowledge=defined_knowledge_data,
        )

        # Phase 17: Build FK-only graph and compute FK/PK diagnostics.
        # RelationshipBuilder operates on the already-scanned schema — no DB call.
        rel_builder = RelationshipBuilder(
            self.schema,
            semantic_relationships=self.semantic_relationships,
        )
        self.fk_graph = rel_builder.get_fk_graph()
        self._fk_diagnostics = rel_builder.get_fk_diagnostics()

        self._loaded = True
        logger.info(
            "SchemaCache loaded for DB '%s': %d tables, %d relationships, %d table profiles "
            "| FK diagnostics: %d PKs, %d FK columns, %d verified relationships, %d isolated tables",
            db_context.active_database,
            len(self.tables),
            len(self.relationships.get("relationships", [])),
            len(self.table_profiles),
            self._fk_diagnostics.get("pk_count", 0),
            self._fk_diagnostics.get("fk_count", 0),
            self._fk_diagnostics.get("verified_relationship_count", 0),
            len(self._fk_diagnostics.get("isolated_tables", [])),
        )

    def is_loaded(self) -> bool:
        return self._loaded

    def get_table(self, name: str) -> Optional[Dict[str, Any]]:
        return self.tables_by_name.get(name) or self.tables_by_name.get(name.lower())

    def get_table_names(self) -> List[str]:
        return list(self.tables_by_name.keys())

    def get_fk_diagnostics(self) -> Dict[str, Any]:
        """
        Phase 17: Return FK/PK/relationship diagnostic counts.
        Useful for monitoring, debugging, and verifying schema refresh.
        """
        return dict(self._fk_diagnostics)

    def get_relationships_for_table(self, table_name: str) -> List[Dict[str, Any]]:
        rels = self.relationships.get("relationships", [])
        tl = table_name.lower()
        return [
            r for r in rels
            if r.get("from_table", "").lower() == tl or r.get("to_table", "").lower() == tl
        ]

    # ------------------------------------------------------------------
    # PRIVATE
    # ------------------------------------------------------------------

    def _require_file(self, name: str) -> None:
        path = self.knowledge_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Required knowledge file missing: {path}\n"
                "Run `python main.py` first to generate the knowledge base."
            )

    def _load_json(self, name: str) -> Dict[str, Any]:
        with open(self.knowledge_dir / name, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_json_optional(self, name: str, default: Any) -> Any:
        path = self.knowledge_dir / name
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not load optional file %s: %s", name, e)
            return default


# Global singleton
schema_cache = SchemaCache()
