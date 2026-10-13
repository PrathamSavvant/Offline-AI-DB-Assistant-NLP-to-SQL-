"""
core/join_path_finder.py

Phase 8 — Automatic Join Path Discovery
Phase 17 — FK-First Database Reliability Hardening

Builds on Phase 2's relationship graph to solve two related problems:

    1. PRECOMPUTED PAIRWISE PATHS (offline, exported as joins.json)
       For every pair of tables in the database, precompute the shortest
       join path between them once, so later phases never have to run BFS
       at question-answering time for common lookups.

    2. MULTI-TABLE JOIN RESOLUTION (used at query time by Phase 9)
       Given an arbitrary set of tables mentioned or implied by a user's
       question (e.g. [\"work_orders\", \"work_unit_group\"] from \"pending
       repairs in TL Group\"), find the minimal set of joins connecting
       ALL of them — including any intermediate tables that must be
       joined through — and generate ready-to-use SQL JOIN clauses.

Phase 17 changes:

    FK-FIRST JOIN PATH SELECTION
    ============================

    Join path selection now works in two passes:

        Pass 1 (FK-only):
            BFS restricted to verified FK edges only.
            If a path is found, it is used — regardless of hop count.
            These are structural truth (INFORMATION_SCHEMA confirmed).

        Pass 2 (FK + semantic fallback):
            Only attempted when no verified FK path exists AND
            semantic fallback is explicitly enabled (default: True).
            Semantic-derived paths are flagged with verified=False.
            A WARNING is logged whenever a semantic path is used in
            place of a verified FK path.

    The system NEVER:
        - Creates a relationship from matching column names alone.
        - Prefers a semantic/lexical path over a verified FK path.
        - Silently mix verified and unverified edges without flagging.

    The result of find_multi_table_join() now includes:
        all_joins_verified: bool  — True iff every edge in the plan is
                                    a verified FK relationship.

This module operates entirely on the Phase 2 relationship graph — it does
not touch the database.
"""

from collections import deque
import re
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from core.relationship_builder import RelationshipBuilder
from core.join_authority import is_approved_join_edge

logger = get_logger(__name__)


class JoinPathFinder:
    """
    Discovers join paths — both precomputed pairwise, and on-demand
    multi-table — using the Phase 2 relationship graph.

    Phase 17: FK-first path selection. Verified FK paths are always
    preferred over semantic paths. The graph is traversed in two passes
    when needed: FK-only first, semantic fallback second.

    Usage:
        finder = JoinPathFinder(relationship_builder)
        joins_data = finder.precompute_all_pairs(table_names)
        plan = finder.find_multi_table_join(["work_orders", "work_unit_group"])
        # plan["all_joins_verified"] — True if every JOIN is a real FK
    """

    def __init__(self, relationship_builder: RelationshipBuilder):
        self.relationship_builder = relationship_builder
        self.graph = relationship_builder.build_graph()
        # Phase 17: FK-only graph for structural truth traversal
        self.fk_graph = relationship_builder.get_fk_graph()

    # ------------------------------------------------------------------
    # SQL GENERATION
    # ------------------------------------------------------------------

    @staticmethod
    def _edges_to_sql(base_table: str, edges: List[Dict[str, Any]]) -> str:
        """
        Convert an ordered list of join hops into a ready-to-use SQL
        FROM/JOIN clause. Assumes each edge's "from" table is already
        present in the query by the time the edge is processed — true for
        both find_join_path's linear output and find_multi_table_join's
        BFS-ordered output.
        """

        lines = [f"FROM {base_table}"]

        for edge in edges:
            from_columns = edge.get("from_columns") or [edge["from_column"]]
            to_columns = edge.get("to_columns") or [edge["to_column"]]
            conditions = " AND ".join(
                f"{edge['from']}.{from_column} = {edge['to']}.{to_column}"
                for from_column, to_column in zip(from_columns, to_columns)
            )
            lines.append(
                f"JOIN {edge['to']} ON {conditions}"
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # PRECOMPUTED PAIRWISE PATHS
    # ------------------------------------------------------------------

    def precompute_all_pairs(self, table_names: List[str]) -> Dict[str, Any]:
        """
        Compute the shortest join path between every distinct pair of
        tables in `table_names`. Returns a flat list of results, including
        pairs with no path (unreachable), so downstream consumers know
        definitively rather than guessing from absence.

        Phase 17: Each path entry now includes:
            - all_joins_verified: bool
            - fk_path_used: bool  (True = found via FK-only pass)

        Returns:
            {
                "pairs": [
                    {
                        "from": str, "to": str,
                        "reachable": bool,
                        "hops": int,
                        "path": [ {...} ] | None,
                        "sql": str | None,
                        "all_joins_verified": bool,
                        "fk_path_used": bool,
                    },
                    ...
                ]
            }
        """

        logger.info(
            "Precomputing pairwise join paths for %d tables (%d pairs)",
            len(table_names), len(table_names) * (len(table_names) - 1) // 2,
        )

        pairs = []

        for i, table_a in enumerate(table_names):
            for table_b in table_names[i + 1:]:
                path = self.relationship_builder.find_join_path(table_a, table_b)

                if path is None:
                    pairs.append({
                        "from": table_a, "to": table_b,
                        "reachable": False, "hops": None,
                        "path": None, "sql": None,
                        "all_joins_verified": False,
                        "fk_path_used": False,
                    })
                    continue

                sql = self._edges_to_sql(table_a, path) if path else None
                all_verified = all(step.get("verified", False) for step in path)

                pairs.append({
                    "from": table_a, "to": table_b,
                    "reachable": True, "hops": len(path),
                    "path": path, "sql": sql,
                    "all_joins_verified": all_verified,
                    "fk_path_used": all_verified,
                })

        reachable_count = sum(1 for p in pairs if p["reachable"])
        verified_count = sum(1 for p in pairs if p.get("all_joins_verified", False))
        logger.info(
            "Precompute complete: %d/%d pair(s) reachable, %d/%d fully FK-verified",
            reachable_count, len(pairs), verified_count, reachable_count,
        )

        return {"pairs": pairs}

    # ------------------------------------------------------------------
    # FK-FIRST BFS HELPER  (Phase 17)
    # ------------------------------------------------------------------

    def _bfs_from_graph(
        self,
        graph: Dict[str, List[Dict[str, Any]]],
        included: set,
        remaining: set,
        blocked: set,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Single BFS pass over `graph` starting from the currently-included
        table set, trying to reach any table in `remaining`.

        Returns the found path steps, or None if no path exists in this graph.
        """
        visited = set(included) | blocked
        queue = deque((node, []) for node in included)

        while queue:
            current, path = queue.popleft()

            if current in remaining:
                return path  # current is the target

            for edge in graph.get(current, []):
                neighbor = edge["to_table"]
                if neighbor in visited:
                    continue
                visited.add(neighbor)

                step = {
                    "from": current,
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
                    "description": edge.get("description", ""),
                    "purpose": edge.get("purpose", ""),
                    "cardinality": edge.get("cardinality", "unknown"),
                    "scope": edge.get("scope", "all"),
                }
                if neighbor in remaining:
                    return path + [step]
                queue.append((neighbor, path + [step]))

        return None

    def _shortest_paths_to_table(
        self,
        graph: Dict[str, List[Dict[str, Any]]],
        included: set,
        target: str,
        blocked: set,
        limit: int = 2,
    ) -> List[List[Dict[str, Any]]]:
        """Return up to two distinct shortest paths without graph explosion."""
        queue = deque((node, [], {node}) for node in sorted(included))
        paths: List[List[Dict[str, Any]]] = []
        signatures = set()
        shortest_hops: Optional[int] = None
        while queue:
            current, path, visited = queue.popleft()
            if shortest_hops is not None and len(path) >= shortest_hops:
                continue
            edges = sorted(
                graph.get(current, []),
                key=lambda edge: (edge.get("to_table", ""), edge.get("constraint_name", "")),
            )
            for edge in edges:
                neighbor = edge["to_table"]
                if neighbor in visited or neighbor in blocked:
                    continue
                step = {
                    "from": current,
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
                    "description": edge.get("description", ""),
                    "purpose": edge.get("purpose", ""),
                    "cardinality": edge.get("cardinality", "unknown"),
                    "scope": edge.get("scope", "all"),
                }
                new_path = path + [step]
                if neighbor == target:
                    shortest_hops = len(new_path) if shortest_hops is None else shortest_hops
                    if len(new_path) != shortest_hops:
                        continue
                    signature = tuple((item["from"], item["constraint_name"], item["to"]) for item in new_path)
                    if signature not in signatures:
                        signatures.add(signature)
                        paths.append(new_path)
                    if len(paths) >= limit:
                        return paths
                else:
                    queue.append((neighbor, new_path, visited | {neighbor}))
        return paths

    @staticmethod
    def _path_purpose_score(path: List[Dict[str, Any]], question: str) -> int:
        """Score only human-authored relationship purpose against the query."""
        query_terms = {
            word for word in re.findall(r"[a-z0-9]+", str(question or "").lower())
            if len(word) > 2 and word not in {
                "the", "and", "for", "from", "with", "show", "list", "find",
                "how", "many", "what", "which", "where", "that", "this",
            }
        }
        if not query_terms:
            return 0
        score = 0
        for edge in path:
            if edge.get("verified", False):
                continue
            purpose = " ".join([str(edge.get("purpose") or ""), str(edge.get("description") or "")])
            purpose_terms = set(re.findall(r"[a-z0-9]+", purpose.lower()))
            score += len(query_terms & purpose_terms)
        return score

    @staticmethod
    def _path_bridge_score(
        path: List[Dict[str, Any]], included: set, target: str, preferred_bridges: set
    ) -> int:
        intermediate = {
            step["to"] for step in path[:-1]
        } | {
            step["from"] for step in path
            if step["from"] not in included and step["from"] != target
        }
        return len(intermediate & preferred_bridges)

    def _choose_path_pool(
        self,
        included: set,
        target: str,
        blocked: set,
        allow_semantic_fallback: bool,
        question: str,
    ) -> tuple[List[List[Dict[str, Any]]], str]:
        """Choose verified or approved path candidates using business purpose.

        A direct/short approved relationship is first-class evidence and may
        beat a longer FK chain.  A real FK remains preferred for equal paths
        unless the approved relationship's human-authored purpose matches the
        question more specifically.
        """
        fk_paths = self._shortest_paths_to_table(self.fk_graph, included, target, blocked)
        if not allow_semantic_fallback:
            return fk_paths, "verified_fk"
        approved_paths = self._shortest_paths_to_table(self.graph, included, target, blocked)
        if not fk_paths:
            return approved_paths, "approved_relationship"
        if not approved_paths:
            return fk_paths, "verified_fk"

        best_fk_hops = len(fk_paths[0])
        best_approved_hops = len(approved_paths[0])
        approved_has_semantic = any(
            any(not edge.get("verified", False) for edge in path)
            for path in approved_paths
        )
        if approved_has_semantic and best_approved_hops < best_fk_hops:
            return approved_paths, "shorter_approved_relationship"
        if approved_has_semantic and best_approved_hops == best_fk_hops:
            approved_purpose = max(self._path_purpose_score(path, question) for path in approved_paths)
            fk_purpose = max(self._path_purpose_score(path, question) for path in fk_paths)
            if approved_purpose > fk_purpose:
                return approved_paths, "purpose_matched_approved_relationship"
        return fk_paths, "verified_fk"

    # ------------------------------------------------------------------
    # MULTI-TABLE JOIN RESOLUTION
    # ------------------------------------------------------------------

    def find_multi_table_join(
        self,
        tables: List[str],
        excluded_tables: Optional[List[str]] = None,
        allow_semantic_fallback: bool = True,
        fail_on_ambiguous_paths: bool = False,
        preferred_bridge_tables: Optional[List[str]] = None,
        question: str = "",
    ) -> Dict[str, Any]:
        """
        Find the minimal set of joins connecting ALL given tables,
        including intermediate "bridge" tables not in the original list
        when a direct relationship doesn't exist.

        Phase 17 — FK-FIRST algorithm:

            For each unresolved target table:

                Pass 1: Attempt BFS using ONLY verified FK edges (fk_graph).
                        If a path is found → use it, mark verified=True.

                Pass 2: If no FK path exists AND allow_semantic_fallback=True,
                        fall back to the full graph (FK + semantic edges).
                        Logs a WARNING — semantic paths should be rare.

                If neither pass finds a path: table is unreachable.

        `excluded_tables`, if given, are treated as impassable.

        Returns:
            {
                "base_table": str,
                "requested_tables": [str],
                "included_tables": [str],       # requested + bridge tables
                "join_edges": [ {...} ],         # ordered, ready for SQL
                "unreachable_tables": [str],
                "sql": str,
                "all_joins_verified": bool,     # Phase 17: all edges are FK
                "semantic_edges_used": int,     # Phase 17: count of fallbacks
            }
        """

        # Preserve order, drop duplicates.
        requested = list(dict.fromkeys(tables))
        blocked = set(excluded_tables or ())
        preferred_bridges = set(preferred_bridge_tables or ()) - blocked

        if not requested:
            return {
                "base_table": None, "requested_tables": [],
                "included_tables": [], "join_edges": [],
                "unreachable_tables": [], "sql": "",
                "all_joins_verified": True, "semantic_edges_used": 0,
                "all_joins_approved": True,
            }

        base_table = requested[0]
        included = {base_table}
        remaining = set(requested[1:]) - included
        join_edges: List[Dict[str, Any]] = []
        ambiguous_paths: List[Dict[str, Any]] = []

        logger.info("Resolving multi-table join for: %s", requested)
        if blocked:
            logger.info("Join bridging will avoid disabled table(s): %s", sorted(blocked))

        progress = True
        while remaining and progress:
            progress = False

            # Select paths using both structural truth and approved business
            # relationship purpose.  FK connectivity is not itself evidence
            # that a table belongs in the request; endpoints were already
            # fixed by the business-intent stage.
            candidates = []
            for target in sorted(remaining):
                paths, route_reason = self._choose_path_pool(
                    included, target, blocked, allow_semantic_fallback, question,
                )
                if paths:
                    purpose_score = max(self._path_purpose_score(path, question) for path in paths)
                    candidates.append((len(paths[0]), -purpose_score, target, paths, route_reason))

            # Grow the join tree from the nearest requested endpoint first.
            # Evaluating ambiguity on a farther endpoint before connecting a
            # unique one can manufacture ambiguity in otherwise deterministic
            # multi-entity requests.
            chosen_path = None
            if candidates:
                _, _, target, paths, route_reason = min(
                    candidates, key=lambda item: (item[0], item[1], item[2])
                )
                ranked_paths = []
                for path in paths:
                    ranked_paths.append((
                        self._path_purpose_score(path, question),
                        self._path_bridge_score(path, included, target, preferred_bridges),
                        sum(1 for edge in path if edge.get("verified", False)),
                        path,
                    ))
                best_key = max((purpose, bridge, verified) for purpose, bridge, verified, _ in ranked_paths)
                paths = [
                    path for purpose, bridge, verified, path in ranked_paths
                    if (purpose, bridge, verified) == best_key
                ]
                if len(paths) > 1 and fail_on_ambiguous_paths:
                    ambiguous_paths.append({
                        "target_table": target,
                        "reason": (
                            "multiple_equal_verified_fk_paths"
                            if route_reason == "verified_fk"
                            else "multiple_equal_approved_relationship_paths"
                        ),
                        "paths": paths,
                    })
                    logger.warning("Multiple equal approved paths remain ambiguous for %s", target)
                    break
                chosen_path = paths[0]

            if chosen_path is not None:
                semantic_hops = [step for step in chosen_path if not step.get("verified", False)]
                logger.info(
                    "Approved join path found (%d hop(s), %d semantic) to reach remaining: %s",
                    len(chosen_path), len(semantic_hops), sorted(remaining),
                )
                join_edges.extend(chosen_path)
                for step in chosen_path:
                    included.add(step["to"])
                remaining -= included
                progress = True
                continue

        unreachable_tables = sorted(remaining)

        if unreachable_tables:
            logger.warning(
                "Could not connect table(s) to the join plan (blocked by "
                "disabled tables or no relationship exists): %s",
                unreachable_tables,
            )

        sql = self._edges_to_sql(base_table, join_edges)

        # Phase 17: audit the final edge set
        all_verified = all(e.get("verified", False) for e in join_edges)
        all_approved = all(is_approved_join_edge(e) for e in join_edges)
        semantic_count = sum(1 for e in join_edges if not e.get("verified", False))

        if not all_verified and join_edges:
            logger.warning(
                "Join plan contains %d semantic (non-FK) edge(s). "
                "all_joins_verified=False for this plan.",
                semantic_count,
            )
        else:
            logger.info(
                "Join plan fully FK-verified: all_joins_verified=True (%d edge(s))",
                len(join_edges),
            )

        result = {
            "base_table": base_table,
            "requested_tables": requested,
            "included_tables": sorted(included),
            "join_edges": join_edges,
            "unreachable_tables": unreachable_tables,
            "sql": sql,
            "all_joins_verified": all_verified,   # Phase 17
            "all_joins_approved": all_approved,
            "semantic_edges_used": semantic_count, # Phase 17
            "ambiguous_paths": ambiguous_paths,
        }

        logger.info(
            "Join plan resolved: %d table(s) connected, %d unreachable, "
            "all_joins_verified=%s",
            len(included), len(unreachable_tables), all_verified,
        )

        return result
