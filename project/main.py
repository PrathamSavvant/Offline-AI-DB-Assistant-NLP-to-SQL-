"""
main.py

Phase 1 entry point — AI Database Understanding Engine.

Runs the full Phase 1-10 pipeline:

    1.  Connect to MySQL
    2.  Scan the database (core/scanner.py)
    3.  Export the knowledge base to disk (core/exporter.py)
    4.  Close the connection
    ... phases 2-10 as before ...

Schema fingerprinting (--force to bypass):
    After Phase 1 the schema is hashed.  If the hash matches the one saved
    from the last crawl, phases 2-10 are skipped — the on-disk knowledge base
    is still valid and there is nothing to rebuild.  Pass --force to
    unconditionally rebuild everything regardless of the hash.

Run with:
    python main.py          # skip phases 2-10 if schema unchanged
    python main.py --force  # always rebuild all phases
"""

import argparse
import sys
import time
from typing import Dict

from config.settings import settings
from core.db_context import db_context
from core.business_dictionary import business_dictionary
from core.database import Database
from core.embedding_search import EmbeddingSearch
from core.exporter import KnowledgeExporter
from core.logger import get_logger
from core.graph_visualizer import GraphVisualizer
from core.importance_ranker import ImportanceRanker
from core.join_path_finder import JoinPathFinder
from core.mindmap_generator import MindMapGenerator
from core.pending_dictionary import pending_dictionary
from core.relationship_builder import RelationshipBuilder
from core import schema_fingerprint
from core.database_registry import database_registry
from core.scanner import DatabaseScanner
from core.semantic_analyzer import SemanticAnalyzer
from core.table_grouper import TableGrouper
from core.table_knowledge import TableKnowledgeBuilder, TableKnowledgeRepository
from core.defined_knowledge import DefinedKnowledgeRepository

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------

def _tick() -> float:
    """Return current time in seconds (high-resolution)."""
    return time.perf_counter()


def _print_phase_timings(phase_times: Dict[str, float]) -> None:
    """Print a formatted table of per-phase elapsed times."""
    label_width = max(len(k) for k in phase_times) + 2
    total = sum(phase_times.values())

    print("\nPhase timings:")
    print("  " + "-" * (label_width + 12))
    for phase, elapsed in phase_times.items():
        bar = "  <-- likely bottleneck" if elapsed == max(phase_times.values()) and elapsed > 1.0 else ""
        print(f"  {phase:<{label_width}}: {elapsed:7.2f} s{bar}")
    print("  " + "-" * (label_width + 12))
    print(f"  {'TOTAL':<{label_width}}: {total:7.2f} s")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_phase_1(force: bool = False) -> None:
    phase_times: Dict[str, float] = {}
    db = Database()

    try:
        db.connect()

        logger.info("Beginning Phase 1 scan of database '%s'", db_context.active_database)

        # ------------------------------------------------------------------
        # Load (or create) the database identity registry so we can recognise
        # this database on reconnect and preserve all manual knowledge.
        # ------------------------------------------------------------------
        database_registry.load()
        database_registry.touch_connected()

        # ------------------------------------------------------------------
        # Phase 1: crawl
        # ------------------------------------------------------------------
        t0 = _tick()
        scanner = DatabaseScanner(db, database_name=db_context.active_database)
        # crawl() wraps the original scan_database() with schema-change
        # detection and unknown-abbreviation detection (see core/scanner.py).
        # schema.json still ends up identical to what scan_database() alone
        # would have produced — nothing about Phase 1's output changed.
        crawl_result = scanner.crawl()
        schema = crawl_result["schema"]
        schema_diff = crawl_result["schema_diff"]
        new_pending = crawl_result["new_pending_abbreviations"]
        phase_times["Phase 1 (crawl)"] = _tick() - t0

        exporter = KnowledgeExporter(output_dir=str(db_context.get_knowledge_dir()))
        exporter.export_all(schema)

        print("\nPhase 1 complete.")
        print(f"  Tables scanned   : {len(schema['tables'])}")
        print(f"  Views scanned    : {len(schema['views'])}")
        print(f"  Triggers scanned : {len(schema['triggers'])}")
        print(f"  Procedures found : {len(schema['procedures'])}")
        print(f"  Knowledge written to ./{db_context.get_knowledge_dir()}/")

        if schema_diff.get("has_changes"):
            print("  Schema changes since last crawl:")
            print(f"    + tables : {schema_diff['added_tables']}")
            print(f"    - tables : {schema_diff['removed_tables']}")
            print(f"    + columns: {schema_diff['added_columns']}")
            print(f"    - columns: {schema_diff['removed_columns']}")
        else:
            print("  Schema changes since last crawl: none")

        if new_pending:
            print(f"  New unknown abbreviation(s) found: {new_pending}")
            print("  Review them on the Pending Dictionary page.")

        # ------------------------------------------------------------------
        # Schema fingerprint check — skip phases 2-10 if unchanged
        # ------------------------------------------------------------------
        current_fp = schema_fingerprint.compute(schema)
        fingerprint_path = db_context.get_knowledge_dir() / "schema_fingerprint.json"
        stored_fp = schema_fingerprint.load(fingerprint_path)

        if not force and current_fp == stored_fp:
            print(
                "\n[OK] Schema fingerprint unchanged - phases 2-10 are up to date.\n"
                "  (Run  python main.py --force  to rebuild everything anyway.)"
            )
            _print_phase_timings(phase_times)
            return

        if force:
            print("\n--force flag set — rebuilding all phases regardless of fingerprint.")
        else:
            reason = "first crawl" if stored_fp is None else "schema has changed"
            print(f"\nSchema fingerprint: {reason} — rebuilding phases 2-10.")

        # ------------------------------------------------------------------
        # Phase 2: relationships
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 2 relationship build")
        t0 = _tick()
        relationship_builder = RelationshipBuilder(schema)
        relationship_data = relationship_builder.build_all()
        exporter.export_relationships(relationship_data)
        phase_times["Phase 2 (relationships)"] = _tick() - t0

        print("\nPhase 2 complete.")
        print(f"  Relationships found : {len(relationship_data['relationships'])}")
        print(f"  Isolated tables     : {len(relationship_data['isolated_tables'])}")
        print("  Knowledge written to ./knowledge/relationships.json and ./knowledge/graph.json")

        # ------------------------------------------------------------------
        # Phase 3: table grouping
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 3 table grouping")
        t0 = _tick()
        table_grouper = TableGrouper(schema)
        grouping_data = table_grouper.build_all()
        exporter.export_table_groups(grouping_data)
        phase_times["Phase 3 (grouping)"] = _tick() - t0

        print("\nPhase 3 complete.")
        print(f"  Domains found       : {len(grouping_data['groups'])}")
        print(f"  Uncategorized tables: {grouping_data['uncategorized_count']}")
        print("  Knowledge written to ./knowledge/table_groups.json")

        # ------------------------------------------------------------------
        # Phase 4: importance ranking
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 4 importance ranking")
        t0 = _tick()
        importance_ranker = ImportanceRanker(schema, relationship_data["graph"])
        ranking_data = importance_ranker.build_all()
        exporter.export_importance_rankings(ranking_data)
        phase_times["Phase 4 (ranking)"] = _tick() - t0

        print("\nPhase 4 complete.")
        top_table = ranking_data["rankings"][0]["table"] if ranking_data["rankings"] else "N/A"
        print(f"  Tables ranked       : {len(ranking_data['rankings'])}")
        print(f"  Most important table: {top_table}")
        print("  Knowledge written to ./knowledge/table_importance.json")

        # ------------------------------------------------------------------
        # Phase 5: mind map
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 5 mind map generation")
        t0 = _tick()
        mindmap_generator = MindMapGenerator(grouping_data, ranking_data)
        mindmap_data = mindmap_generator.build_all()
        exporter.export_mindmap(mindmap_data)
        phase_times["Phase 5 (mindmap)"] = _tick() - t0

        print("\nPhase 5 complete.")
        print(f"  Domains in mind map : {len(mindmap_data['tree']['children'])}")
        print("  Knowledge written to ./knowledge/mindmap.md and ./knowledge/mindmap.json")
        try:
            print("\n" + mindmap_data["ascii"])
        except UnicodeEncodeError:
            print("\n" + mindmap_data["ascii"].encode('ascii', errors='backslashreplace').decode('ascii'))

        # ------------------------------------------------------------------
        # Phase 6: graph visualization
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 6 graph visualization generation")
        t0 = _tick()
        graph_visualizer = GraphVisualizer(relationship_data)
        graph_formats = graph_visualizer.build_all()
        exporter.export_graph_visualizations(graph_formats)
        phase_times["Phase 6 (graph viz)"] = _tick() - t0

        print("\nPhase 6 complete.")
        print("  Formats generated   : Mermaid, Graphviz, PlantUML, Draw.io CSV")
        print("  Knowledge written to ./knowledge/graph.mmd, graph.dot, graph.puml, graph_drawio.csv")

        # ------------------------------------------------------------------
        # Phase 7: business meaning detection
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 7 business meaning detection")
        t0 = _tick()
        semantic_analyzer = SemanticAnalyzer(schema, grouping_data, dictionary=business_dictionary)
        semantics_data = semantic_analyzer.build_all()
        exporter.export_business_semantics(semantics_data)
        phase_times["Phase 7 (semantics)"] = _tick() - t0

        print("\nPhase 7 complete.")
        print(f"  Tables analyzed     : {len(semantics_data['tables'])}")
        print("  Knowledge written to ./knowledge/business_semantics.json")

        # ------------------------------------------------------------------
        # Phase 8: join path discovery
        # ------------------------------------------------------------------
        logger.info("Beginning Phase 8 join path discovery")
        t0 = _tick()
        join_finder = JoinPathFinder(relationship_builder)
        table_names = [t["name"] for t in schema["tables"]]
        if len(table_names) <= settings.MAX_PRECOMPUTED_JOIN_TABLES:
            joins_data = join_finder.precompute_all_pairs(table_names)
        else:
            # Query-time join planning is graph-based and bounded by the few
            # selected context tables. Avoid persisting O(n²) pair data.
            joins_data = {"pairs": [], "skipped": True, "table_count": len(table_names)}
            logger.info(
                "Skipping pairwise join precomputation for %d tables (limit=%d)",
                len(table_names), settings.MAX_PRECOMPUTED_JOIN_TABLES,
            )
        exporter.export_joins(joins_data)
        phase_times["Phase 8 (joins)"] = _tick() - t0

        reachable_pairs = sum(1 for p in joins_data["pairs"] if p["reachable"])
        print("\nPhase 8 complete.")
        print(f"  Table pairs checked : {len(joins_data['pairs'])}")
        if joins_data.get("skipped"):
            print("  Pairwise precomputation skipped; request-time graph planning remains enabled.")
        print(f"  Reachable pairs     : {reachable_pairs}")
        print("  Knowledge written to ./knowledge/joins.json")

        # ------------------------------------------------------------------
        # Persistent table profiles — incremental per-table update.
        #
        # Each profile stores its own table_fingerprint.  We compare that
        # stored fingerprint with the current structural hash of every table
        # and only pass the changed table names to build_all().  Unchanged
        # tables are returned as-is; their manual_knowledge is never touched.
        # ------------------------------------------------------------------
        profile_repository = TableKnowledgeRepository(db_context.get_knowledge_dir() / "tables")
        existing_profiles = profile_repository.load_all()
        logger.info(
            "[Registry] Existing knowledge loaded: %d profile(s) on disk",
            len(existing_profiles),
        )

        if force:
            # --force: rebuild every table's system_knowledge unconditionally.
            changed_tables = None
            logger.info(
                "[Profiles] --force flag set — rebuilding system knowledge for all %d table(s)",
                len(schema["tables"]),
            )
        else:
            changed_tables: set = set()
            for tbl in schema["tables"]:
                current_fp = schema_fingerprint.compute_table(tbl)
                stored_fp = existing_profiles.get(tbl["name"], {}).get("table_fingerprint", "")
                if current_fp != stored_fp:
                    changed_tables.add(tbl["name"])
            unchanged_count = len(schema["tables"]) - len(changed_tables)
            if changed_tables:
                logger.info(
                    "[Profiles] Per-table fingerprint: %d table(s) changed, %d unchanged",
                    len(changed_tables),
                    unchanged_count,
                )
            else:
                logger.info(
                    "[Profiles] Per-table fingerprint: all %d table(s) unchanged",
                    len(schema["tables"]),
                )

        profile_builder = TableKnowledgeBuilder(
            schema, semantics_data, relationship_data, grouping_data, ranking_data,
            dictionary=business_dictionary,
        )
        new_profiles = profile_builder.build_all(
            existing_profiles=existing_profiles,
            changed_tables=changed_tables,
        )
        exporter.export_table_profiles(new_profiles)
        logger.info(
            "[Profiles] Manual knowledge preserved for all %d table(s)",
            len(new_profiles),
        )

        # This diagnostic is quadratic/graph-heavy on wide schemas. It is
        # useful for a small demo database, but must never turn a production
        # knowledge rebuild into an all-table join-planning job.
        if 2 <= len(table_names) <= 20:
            demo_plan = join_finder.find_multi_table_join(table_names)
            print("\n  Example multi-table join (all tables):")
            print("  " + demo_plan["sql"].replace("\n", "\n  "))
            if demo_plan["unreachable_tables"]:
                print(f"  Unreachable: {demo_plan['unreachable_tables']}")
        elif len(table_names) > 20:
            print("\n  All-table join demonstration skipped for large schema.")

        # ------------------------------------------------------------------
        # Phase 10: embedding index
        # ------------------------------------------------------------------
        if settings.ENABLE_EMBEDDINGS:
            logger.info("Beginning Phase 10 embedding index build")
            t0 = _tick()
            embedding_search = EmbeddingSearch(schema)
            defined_knowledge = DefinedKnowledgeRepository(
                db_context.get_knowledge_dir() / "defined_knowledge.json"
            ).all()
            embedding_built = embedding_search.build_index(defined_knowledge=defined_knowledge)
            phase_times["Phase 10 (embeddings)"] = _tick() - t0
            print("\nPhase 10 complete.")
            if embedding_built:
                print(f"  Embedding index built for {len(schema['tables'])} table(s)")
                print("  Index written to ./knowledge/embeddings/")
            else:
                print("  Embedding index skipped; preload the local model before enabling it.")
        else:
            print("\nPhase 10 skipped (ENABLE_EMBEDDINGS=false).")

        covered = sum(
            1 for t in schema["tables"]
            if business_dictionary.expand_name(t["name"])["expansions"]
            or any(
                business_dictionary.expand_name(c["name"])["expansions"]
                for c in t.get("columns", [])
            )
        )
        total = len(schema["tables"]) or 1
        print("\nDictionary coverage:")
        print(f"  {covered}/{len(schema['tables'])} table(s) touched by a dictionary "
              f"expansion ({covered / total:.0%})")
        print(f"  Pending (unresolved) abbreviations: {len(pending_dictionary.get_all())}")

        # ------------------------------------------------------------------
        # Save fingerprint and update the database registry now that all
        # phases succeeded.
        # ------------------------------------------------------------------
        schema_fingerprint.save(current_fp, fingerprint_path)
        logger.info("Schema fingerprint saved: %s", current_fp[:12] + "...")

        database_registry.update_after_scan(
            fingerprint=current_fp,
            table_count=len(schema["tables"]),
            profile_count=len(new_profiles),
            status="up_to_date",
        )

    except Exception as e:
        logger.exception("Phase 1 failed: %s", e)
        print(f"\nPhase 1 failed: {e}")
        sys.exit(1)

    finally:
        db.close()
        if phase_times:
            _print_phase_timings(phase_times)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AI Database Understanding Engine — rebuild the knowledge base.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Unconditionally rebuild all phases even if the schema fingerprint "
            "has not changed since the last crawl."
        ),
    )
    args = parser.parse_args()
    run_phase_1(force=args.force)
