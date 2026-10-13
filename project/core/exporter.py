"""
core/exporter.py

Phase 1 — Knowledge Exporter

Takes the structured schema dict produced by DatabaseScanner.scan_database()
and writes it to disk as the knowledge base:

    knowledge/
        schema.json              -> full schema, everything
        database_summary.json    -> lightweight overview / stats
        tables/
            <table_name>.json    -> one file per table

This module has no knowledge of MySQL or INFORMATION_SCHEMA — it only knows
how to serialize the already-assembled schema dict. That separation keeps
scanning and exporting independently testable and reusable in later phases.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from core.logger import get_logger
from core.table_knowledge import TableKnowledgeBuilder, TableKnowledgeRepository

logger = get_logger(__name__)


class KnowledgeExporter:
    """
    Serializes a scanned database schema into the on-disk knowledge base
    used by later phases and, eventually, by the LLM context generator.
    """

    def __init__(self, output_dir: str = "knowledge"):
        self.output_dir = output_dir
        self.tables_dir = os.path.join(output_dir, "tables")

    # ------------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------------

    def export_all(self, schema: Dict[str, Any]) -> None:
        """
        Run every export step. This is the single entry point main.py
        should call.
        """

        self._ensure_directories()

        self.export_schema(schema)
        self.export_summary(schema)
        self.export_tables(schema)

        logger.info("All knowledge exports complete. Output dir: '%s'", self.output_dir)

    def export_schema(self, schema: Dict[str, Any]) -> None:
        """
        Write the complete schema (tables, columns, views, triggers,
        procedures — everything) to knowledge/schema.json.
        """

        path = os.path.join(self.output_dir, "schema.json")
        self._write_json(path, schema)
        logger.info("Exported full schema -> %s", path)

    def export_summary(self, schema: Dict[str, Any]) -> None:
        """
        Write a lightweight, human-skimmable overview of the database to
        knowledge/database_summary.json. This is meant for quick reference
        and for later phases (grouping, ranking) that don't need full
        column-level detail up front.
        """

        tables = schema.get("tables", [])

        summary = {
            "database": schema.get("database"),
            "generated_at": datetime.now().isoformat(),
            "total_tables": len(tables),
            "total_views": len(schema.get("views", [])),
            "total_triggers": len(schema.get("triggers", [])),
            "total_procedures": len(schema.get("procedures", [])),
            "tables_overview": [
                {
                    "name": table["name"],
                    "type": table["type"],
                    "approx_row_count": table.get("approx_row_count"),
                    "column_count": len(table.get("columns", [])),
                    "primary_key": table.get("primary_key", []),
                    "foreign_key_count": len(table.get("foreign_keys", [])),
                    "index_count": len(table.get("indexes", [])),
                }
                for table in tables
            ],
        }

        path = os.path.join(self.output_dir, "database_summary.json")
        self._write_json(path, summary)
        logger.info("Exported database summary -> %s", path)

    def export_relationships(self, relationship_data: Dict[str, Any]) -> None:
        """
        Write Phase 2 output to disk:
            knowledge/relationships.json -> flat list of PK-FK relationships
            knowledge/graph.json         -> full bidirectional adjacency list

        `relationship_data` is the dict returned by
        RelationshipBuilder.build_all().
        """

        self._ensure_directories()

        relationships_path = os.path.join(self.output_dir, "relationships.json")
        self._write_json(relationships_path, {
            "relationships": relationship_data["relationships"],
            "isolated_tables": relationship_data["isolated_tables"],
        })
        logger.info("Exported relationships -> %s", relationships_path)

        graph_path = os.path.join(self.output_dir, "graph.json")
        self._write_json(graph_path, relationship_data["graph"])
        logger.info("Exported relationship graph -> %s", graph_path)

    def export_table_groups(self, grouping_data: Dict[str, Any]) -> None:
        """
        Write Phase 3 output to disk:
            knowledge/table_groups.json -> domains, per-table domain lookup,
                                            and uncategorized count

        `grouping_data` is the dict returned by TableGrouper.build_all().
        """

        self._ensure_directories()

        path = os.path.join(self.output_dir, "table_groups.json")
        self._write_json(path, grouping_data)
        logger.info("Exported table groups -> %s", path)

    def export_importance_rankings(self, ranking_data: Dict[str, Any]) -> None:
        """
        Write Phase 4 output to disk:
            knowledge/table_importance.json -> weights used + full ranked
                                                list with score breakdowns

        `ranking_data` is the dict returned by ImportanceRanker.build_all().
        """

        self._ensure_directories()

        path = os.path.join(self.output_dir, "table_importance.json")
        self._write_json(path, ranking_data)
        logger.info("Exported table importance rankings -> %s", path)

    def export_mindmap(self, mindmap_data: Dict[str, Any]) -> None:
        """
        Write Phase 5 output to disk:
            knowledge/mindmap.md   -> human-readable tree (ASCII + bullets)
            knowledge/mindmap.json -> the nested tree structure, for later
                                      phases (e.g. Context Generator) to
                                      consume programmatically

        `mindmap_data` is the dict returned by MindMapGenerator.build_all().
        """

        self._ensure_directories()

        md_path = os.path.join(self.output_dir, "mindmap.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(mindmap_data["markdown"])
        logger.info("Exported mind map -> %s", md_path)

        json_path = os.path.join(self.output_dir, "mindmap.json")
        self._write_json(json_path, mindmap_data["tree"])
        logger.info("Exported mind map tree -> %s", json_path)

    def export_graph_visualizations(self, graph_formats: Dict[str, str]) -> None:
        """
        Write Phase 6 output to disk, one file per visualization format:
            knowledge/graph.mmd        -> Mermaid flowchart
            knowledge/graph.dot        -> Graphviz DOT
            knowledge/graph.puml       -> PlantUML
            knowledge/graph_drawio.csv -> Draw.io CSV import

        `graph_formats` is the dict returned by GraphVisualizer.build_all().
        """

        self._ensure_directories()

        file_map = {
            "mermaid": "graph.mmd",
            "graphviz": "graph.dot",
            "plantuml": "graph.puml",
            "drawio_csv": "graph_drawio.csv",
        }

        for key, filename in file_map.items():
            path = os.path.join(self.output_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(graph_formats[key])
            logger.info("Exported %s graph -> %s", key, path)

    def export_business_semantics(self, semantics_data: Dict[str, Any]) -> None:
        """
        Write Phase 7 output to disk:
            knowledge/business_semantics.json -> per-table business domain,
                                                  concept tags, and
                                                  column-level semantics

        `semantics_data` is the dict returned by SemanticAnalyzer.build_all().
        """

        self._ensure_directories()

        path = os.path.join(self.output_dir, "business_semantics.json")
        self._write_json(path, semantics_data)
        logger.info("Exported business semantics -> %s", path)

    def export_joins(self, joins_data: Dict[str, Any]) -> None:
        """
        Write Phase 8 output to disk:
            knowledge/joins.json -> precomputed shortest join path (and
                                     ready-to-use SQL) between every pair
                                     of tables in the database

        `joins_data` is the dict returned by
        JoinPathFinder.precompute_all_pairs().
        """

        self._ensure_directories()

        path = os.path.join(self.output_dir, "joins.json")
        self._write_json(path, joins_data)
        logger.info("Exported precomputed join paths -> %s", path)

    def export_tables(self, schema: Dict[str, Any]) -> None:
        """Write baseline persistent profiles during the crawl phase.

        Later phases enrich these same files with semantics, relationships, and
        rankings.  Runtime never needs to turn raw per-table exports into a
        business profile during a request.
        """
        repository = TableKnowledgeRepository(Path(self.tables_dir))
        self.export_table_profiles(TableKnowledgeBuilder(schema).build_all(repository.load_all()))

    def export_table_profiles(self, profiles: Dict[str, Dict[str, Any]]) -> None:
        """Persist the authoritative business profile for every table."""
        TableKnowledgeRepository(Path(self.tables_dir)).save_all(profiles)

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    def _ensure_directories(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        os.makedirs(self.tables_dir, exist_ok=True)

    @staticmethod
    def _write_json(path: str, data: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
