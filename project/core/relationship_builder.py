"""
core/relationship_builder.py

Phase 2 — Relationship Builder
Phase 17 — FK-First Database Reliability Hardening

Takes the schema dict produced by DatabaseScanner.scan_database() (Phase 1)
and builds:

    1. A flat, deduplicated list of all PK-FK relationships in the database.
    2. A bidirectional relationship graph (adjacency list) connecting tables.
    3. Shortest join-path discovery between any two tables via BFS.

Phase 17 additions:

    STRUCTURAL TRUTH SEPARATION
    ============================

    Every edge in the graph now carries two provenance fields:

        provenance  — the data source ("mysql_information_schema" or
                      "user_verified" for semantic relationships)

        verified    — True  iff the edge was sourced from actual database
                              FK metadata (INFORMATION_SCHEMA)
                      False if the edge comes from a user-curated semantic
                              relationship (these are authoritative for
                              join intent but not structural FKs)

    FK-only views
    =============
    get_fk_only_relationships() — only verified=True edges
    get_fk_graph()              — adjacency graph of only verified edges
    get_fk_diagnostics()        — counts for monitoring

This module takes the already-scanned schema as input — it does NOT touch
the database. That keeps Phase 1 (scanning) and Phase 2 (reasoning about
relationships) independently testable.
"""

from collections import deque
from typing import Any, Dict, List, Optional, Set, Tuple

from core.logger import get_logger

logger = get_logger(__name__)


class RelationshipBuilder:
    """
    Builds relationship data structures from an already-scanned schema.

    Usage:
        builder = RelationshipBuilder(schema)
        relationships = builder.get_relationships()
        graph = builder.build_graph()
        fk_graph = builder.get_fk_graph()
        path = builder.find_join_path("work_orders", "work_unit_group")
        diags = builder.get_fk_diagnostics()
    """

    def __init__(self, schema: Dict[str, Any], semantic_relationships: Optional[List[Dict[str, Any]]] = None):
        self.schema = schema
        self.tables = schema.get("tables", [])
        self._graph: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._fk_graph: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self.semantic_relationships = semantic_relationships or []

    # ------------------------------------------------------------------
    # FLAT RELATIONSHIP LIST
    # ------------------------------------------------------------------

    def get_relationships(self) -> List[Dict[str, Any]]:
        """
        Return a flat, deduplicated list of every PK-FK relationship in the
        database, gathered from each table's `foreign_keys` entries.

        Phase 17: Each entry carries explicit `verified` and `provenance`
        fields so downstream consumers can distinguish structural FK truth
        from user-curated semantic relationships.

        Each entry:
            {
                "from_table": str,
                "from_column": str,
                "to_table": str,
                "to_column": str,
                "constraint_name": str,
                "relationship_type": str,   # "foreign_key" | "semantic"
                "provenance": str,          # "mysql_information_schema" | "user_verified"
                "verified": bool,           # True only for database FK metadata
            }
        """

        logger.info("Building flat relationship list")

        relationships = []
        seen: Set[Tuple[str, str, str, str]] = set()

        # --- Database FK metadata (STRUCTURAL TRUTH) ---
        # Group all column pairs belonging to the same FK constraint.  This
        # preserves composite keys as one structural relationship rather than
        # several unrelated, individually-usable joins.
        grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for table in self.tables:
            for fk in table.get("foreign_keys", []):
                key = (fk["table"], fk["constraint_name"], fk["references_table"])
                grouped.setdefault(key, []).append(fk)
        for (from_table, constraint_name, to_table), members in grouped.items():
            members.sort(key=lambda item: (item.get("ordinal_position") or 0, item["column"]))
            from_columns = [item["column"] for item in members]
            to_columns = [item["references_column"] for item in members]
            pair_key = (from_table, "|".join(from_columns), to_table, "|".join(to_columns))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            relationships.append({
                "database": self.schema.get("database", ""),
                "from_table": from_table,
                "from_column": from_columns[0],
                "from_columns": from_columns,
                "to_table": to_table,
                "to_column": to_columns[0],
                "to_columns": to_columns,
                "constraint_name": constraint_name,
                "relationship_type": "foreign_key",
                "provenance": "mysql_information_schema",
                "source": "mysql_information_schema",
                "verified": True,
                "confidence": 1.0,
            })

        # --- User-verified semantic relationships (not structural FK truth) ---
        for semantic in self.semantic_relationships:
            if semantic.get("status") != "approved":
                continue
            from_columns = list(semantic.get("from_columns") or [semantic["from_column"]])
            to_columns = list(semantic.get("to_columns") or [semantic["to_column"]])
            if not from_columns or len(from_columns) != len(to_columns):
                continue
            relationships.append({
                "database": self.schema.get("database", ""),
                "from_table": semantic["from_table"],
                "from_column": from_columns[0],
                "from_columns": from_columns,
                "to_table": semantic["to_table"],
                "to_column": to_columns[0],
                "to_columns": to_columns,
                "constraint_name": semantic["relationship_id"],
                "relationship_type": "semantic",
                "provenance": "user_verified",
                "source": "user_verified",
                "verified": False,          # Phase 17: semantic ≠ FK structural truth
                "approved": True,
                "confidence": float(semantic.get("confidence", 1.0)),
                "description": semantic.get("description", ""),
                "purpose": semantic.get("purpose", ""),
                "cardinality": semantic.get("cardinality", "unknown"),
                "scope": semantic.get("scope", "all"),
            })

        fk_count = sum(1 for r in relationships if r["verified"])
        semantic_count = len(relationships) - fk_count
        logger.info(
            "Found %d unique relationships (%d verified FK, %d semantic)",
            len(relationships), fk_count, semantic_count,
        )
        return relationships

    def get_fk_only_relationships(self) -> List[Dict[str, Any]]:
        """
        Return only verified FK relationships (provenance = mysql_information_schema).

        Phase 17: This is the authoritative structural truth for join planning.
        Used by the SQL validator to enforce that JOINs reference real FK pairs.
        """
        return [r for r in self.get_relationships() if r.get("verified", False)]

    # ------------------------------------------------------------------
    # RELATIONSHIP GRAPH
    # ------------------------------------------------------------------

    def build_graph(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Build a bidirectional adjacency list connecting every table to
        every table it shares a foreign key with — in both directions,
        so join-path discovery can traverse the schema regardless of
        which side "owns" the foreign key.

        Phase 17: Every edge in the graph carries:
            - relationship_type: "foreign_key" | "semantic"
            - provenance:        "mysql_information_schema" | "user_verified"
            - verified:          True for real FK metadata, False for semantic

        Structure:
            {
                "work_orders": [
                    {
                        "to_table": "work_unit",
                        "via_column": "unit_id",
                        "to_column": "unit_id",
                        "constraint_name": "fk_work_orders_unit",
                        "direction": "outgoing",
                        "relationship_type": "foreign_key",
                        "provenance": "mysql_information_schema",
                        "verified": True,
                    },
                    ...
                ],
            }

        Result is cached on the instance after the first call.
        """

        if self._graph is not None:
            return self._graph

        logger.info("Building relationship graph")

        graph: Dict[str, List[Dict[str, Any]]] = {
            table["name"]: [] for table in self.tables
        }

        for rel in self.get_relationships():
            from_table = rel["from_table"]
            to_table = rel["to_table"]

            # Some FKs may reference tables outside the scanned set
            # (rare, but be defensive rather than raising).
            graph.setdefault(from_table, [])
            graph.setdefault(to_table, [])

            graph[from_table].append({
                "to_table": to_table,
                "via_column": rel["from_column"],
                "to_column": rel["to_column"],
                "via_columns": list(rel.get("from_columns", [rel["from_column"]])),
                "to_columns": list(rel.get("to_columns", [rel["to_column"]])),
                "constraint_name": rel["constraint_name"],
                "direction": "outgoing",
                "relationship_type": rel.get("relationship_type", "foreign_key"),
                "provenance": rel.get("provenance", "mysql_information_schema"),
                "verified": rel.get("verified", False),
                "approved": rel.get("approved", False),
                "database": rel.get("database", ""),
                "confidence": rel.get("confidence", 1.0),
                "description": rel.get("description", ""),
                "purpose": rel.get("purpose", ""),
                "cardinality": rel.get("cardinality", "unknown"),
                "scope": rel.get("scope", "all"),
            })

            graph[to_table].append({
                "to_table": from_table,
                "via_column": rel["to_column"],
                "to_column": rel["from_column"],
                "via_columns": list(rel.get("to_columns", [rel["to_column"]])),
                "to_columns": list(rel.get("from_columns", [rel["from_column"]])),
                "constraint_name": rel["constraint_name"],
                "direction": "incoming",
                "relationship_type": rel.get("relationship_type", "foreign_key"),
                "provenance": rel.get("provenance", "mysql_information_schema"),
                "verified": rel.get("verified", False),
                "approved": rel.get("approved", False),
                "database": rel.get("database", ""),
                "confidence": rel.get("confidence", 1.0),
                "description": rel.get("description", ""),
                "purpose": rel.get("purpose", ""),
                "cardinality": rel.get("cardinality", "unknown"),
                "scope": rel.get("scope", "all"),
            })

        self._graph = graph
        logger.info("Graph built with %d nodes", len(graph))
        return graph

    def get_fk_graph(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Phase 17: Return a graph view containing ONLY verified FK edges.

        This is the structural authority for join path validation.
        Column-name similarity, domain heuristics, and semantic relationships
        are never present here — only actual database FK constraints.
        """
        if self._fk_graph is not None:
            return self._fk_graph

        full_graph = self.build_graph()
        fk_graph: Dict[str, List[Dict[str, Any]]] = {}
        for table_name, edges in full_graph.items():
            fk_graph[table_name] = [e for e in edges if e.get("verified", False)]

        self._fk_graph = fk_graph
        fk_edge_count = sum(len(edges) for edges in fk_graph.values())
        logger.info(
            "FK-only graph built: %d nodes, %d directed edges",
            len(fk_graph), fk_edge_count,
        )
        return fk_graph

    # ------------------------------------------------------------------
    # JOIN PATH DISCOVERY
    # ------------------------------------------------------------------

    def find_join_path(
        self, start_table: str, end_table: str
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Find the shortest join path between two tables using BFS over the
        relationship graph. Returns an ordered list of join steps, e.g.:

            [
                {"from": "work_orders", "from_column": "unit_id",
                 "to": "work_unit", "to_column": "unit_id",
                 "relationship_type": "foreign_key",
                 "provenance": "mysql_information_schema",
                 "verified": True},
                ...
            ]

        Returns:
            - [] if start_table == end_table (no join needed)
            - None if no path exists between the two tables
        """

        graph = self.build_graph()

        if start_table not in graph or end_table not in graph:
            logger.warning(
                "Join path requested for unknown table(s): %s -> %s",
                start_table, end_table,
            )
            return None

        if start_table == end_table:
            return []

        # BFS: each queue entry is (current_table, path_so_far)
        visited = {start_table}
        queue = deque([(start_table, [])])

        while queue:
            current_table, path = queue.popleft()

            for edge in graph.get(current_table, []):
                neighbor = edge["to_table"]

                if neighbor in visited:
                    continue

                step = {
                    "from": current_table,
                    "from_column": edge["via_column"],
                    "from_columns": list(edge.get("via_columns", [edge["via_column"]])),
                    "to": neighbor,
                    "to_column": edge["to_column"],
                    "to_columns": list(edge.get("to_columns", [edge["to_column"]])),
                    "constraint_name": edge.get("constraint_name", ""),
                    "relationship_type": edge.get("relationship_type", "foreign_key"),
                    "provenance": edge.get("provenance", "mysql_information_schema"),
                    "source": edge.get("provenance", "mysql_information_schema"),
                    "verified": edge.get("verified", False),
                    "approved": edge.get("approved", False),
                    "database": edge.get("database", ""),
                    "confidence": edge.get("confidence", 1.0),
                }
                new_path = path + [step]

                if neighbor == end_table:
                    logger.info(
                        "Join path found: %s -> %s (%d hop(s))",
                        start_table, end_table, len(new_path),
                    )
                    return new_path

                visited.add(neighbor)
                queue.append((neighbor, new_path))

        logger.warning("No join path found between %s and %s", start_table, end_table)
        return None

    # ------------------------------------------------------------------
    # FK DIAGNOSTICS (Phase 17)
    # ------------------------------------------------------------------

    def get_fk_diagnostics(self) -> Dict[str, Any]:
        """
        Phase 17: Return FK/PK statistics for monitoring and debugging.

        Returns:
            {
                "table_count":               int,
                "pk_count":                  int,   # tables with a primary key
                "fk_count":                  int,   # total FK columns
                "verified_relationship_count": int, # unique verified FK edges
                "semantic_relationship_count": int, # user-curated semantic edges
                "isolated_tables":           [str], # tables with zero relationships
            }
        """
        pk_count = sum(1 for t in self.tables if t.get("primary_key"))
        fk_count = sum(len(t.get("foreign_keys", [])) for t in self.tables)
        all_rels = self.get_relationships()
        verified = [r for r in all_rels if r.get("verified", False)]
        semantic = [r for r in all_rels if not r.get("verified", False)]
        graph = self.build_graph()
        isolated = [name for name, edges in graph.items() if not edges]

        diags = {
            "table_count": len(self.tables),
            "pk_count": pk_count,
            "fk_count": fk_count,
            "verified_relationship_count": len(verified),
            "semantic_relationship_count": len(semantic),
            "isolated_tables": isolated,
        }
        logger.info(
            "FK diagnostics: %d tables, %d PKs, %d FK columns, "
            "%d verified relationships, %d semantic, %d isolated",
            diags["table_count"], diags["pk_count"], diags["fk_count"],
            diags["verified_relationship_count"], diags["semantic_relationship_count"],
            len(isolated),
        )
        return diags

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, Any]:
        """
        Run the full Phase 2 build and return everything downstream
        phases / the exporter will need:

            {
                "relationships": [ {...} ],
                "graph": { table: [ {...} ] },
                "fk_graph": { table: [ {...} ] },   # Phase 17: FK-only view
                "isolated_tables": [str],
                "fk_diagnostics": {...},            # Phase 17: counts
            }
        """

        logger.info("Starting Phase 2 relationship build")

        relationships = self.get_relationships()
        graph = self.build_graph()
        fk_graph = self.get_fk_graph()
        diagnostics = self.get_fk_diagnostics()

        isolated_tables = [
            table_name for table_name, edges in graph.items() if not edges
        ]

        if isolated_tables:
            logger.info(
                "%d table(s) have no foreign key relationships: %s",
                len(isolated_tables), isolated_tables,
            )

        result = {
            "relationships": relationships,
            "graph": graph,
            "fk_graph": fk_graph,
            "isolated_tables": isolated_tables,
            "fk_diagnostics": diagnostics,
        }

        logger.info(
            "Phase 2 complete: %d relationships (%d verified FK), "
            "%d tables in graph, %d isolated",
            len(relationships), diagnostics["verified_relationship_count"],
            len(graph), len(isolated_tables),
        )

        return result
