"""
core/importance_ranker.py

Phase 4 — Table Importance Ranking

Ranks every table in the database by a weighted combination of:
    1. Incoming foreign key count  (how many other tables reference it —
       the strongest signal of centrality; heavily-referenced tables are
       usually "core" entities like customers, users, products)
    2. Outgoing foreign key count  (how many other tables it references)
    3. Approximate row count       (bigger tables tend to be transactional
       / operationally central rather than small lookup tables)
    4. Business keyword match      (table name matches a known "core
       entity" keyword from config/importance_keywords.py)

Each raw metric is min-max normalized across all tables (0-1) before being
combined, so no single metric with a large numeric range (e.g. row count)
dominates the score just because of its scale.

This module takes the already-scanned schema and the Phase 2 relationship
graph as input — it does NOT touch the database or rebuild the graph.
"""

from typing import Any, Dict, List, Optional

from config.importance_keywords import IMPORTANCE_KEYWORDS
from core.logger import get_logger

logger = get_logger(__name__)

# Default weights — must sum to 1.0 for scores to land in a clean 0-1 range,
# though the code doesn't strictly enforce that.
DEFAULT_WEIGHTS = {
    "incoming_fk": 0.4,
    "outgoing_fk": 0.2,
    "row_count": 0.2,
    "keyword": 0.2,
}


class ImportanceRanker:
    """
    Ranks tables by importance using FK connectivity, size, and keyword
    signals.

    Usage:
        ranker = ImportanceRanker(schema, graph)
        result = ranker.build_all()
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        graph: Dict[str, List[Dict[str, Any]]],
        weights: Optional[Dict[str, float]] = None,
    ):
        self.schema = schema
        self.tables = schema.get("tables", [])
        self.graph = graph
        self.weights = weights if weights is not None else DEFAULT_WEIGHTS

    # ------------------------------------------------------------------
    # RAW METRICS
    # ------------------------------------------------------------------

    def _compute_raw_metrics(self) -> Dict[str, Dict[str, Any]]:
        """
        Gather the raw, un-normalized metrics for every table.

        Returns:
            {
                table_name: {
                    "incoming_fk": int,
                    "outgoing_fk": int,
                    "row_count": int,
                    "keyword_match": bool,
                }
            }
        """

        raw: Dict[str, Dict[str, Any]] = {}

        for table in self.tables:
            table_name = table["name"]
            edges = self.graph.get(table_name, [])

            incoming = sum(1 for e in edges if e["direction"] == "incoming")
            outgoing = sum(1 for e in edges if e["direction"] == "outgoing")

            row_count = table.get("approx_row_count") or 0

            name_lower = table_name.lower()
            keyword_match = any(kw.lower() in name_lower for kw in IMPORTANCE_KEYWORDS)

            raw[table_name] = {
                "incoming_fk": incoming,
                "outgoing_fk": outgoing,
                "row_count": row_count,
                "keyword_match": keyword_match,
            }

        return raw

    # ------------------------------------------------------------------
    # NORMALIZATION
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(values: Dict[str, float]) -> Dict[str, float]:
        """
        Min-max normalize a table_name -> value mapping into 0-1.
        If all values are equal (including all zero), every table gets 0.0
        to avoid division by zero and to avoid implying false differences.
        """

        if not values:
            return {}

        min_val = min(values.values())
        max_val = max(values.values())

        if max_val == min_val:
            return {k: 0.0 for k in values}

        return {
            k: (v - min_val) / (max_val - min_val)
            for k, v in values.items()
        }

    # ------------------------------------------------------------------
    # RANKING
    # ------------------------------------------------------------------

    def rank_tables(self) -> List[Dict[str, Any]]:
        """
        Compute the final weighted importance score for every table and
        return them sorted descending by score, each with rank position
        and a full breakdown of contributing metrics for transparency.
        """

        logger.info("Ranking %d tables by importance", len(self.tables))

        raw = self._compute_raw_metrics()

        incoming_norm = self._normalize(
            {t: m["incoming_fk"] for t, m in raw.items()}
        )
        outgoing_norm = self._normalize(
            {t: m["outgoing_fk"] for t, m in raw.items()}
        )
        row_count_norm = self._normalize(
            {t: m["row_count"] for t, m in raw.items()}
        )

        scored = []
        for table_name, metrics in raw.items():
            keyword_score = 1.0 if metrics["keyword_match"] else 0.0

            score = (
                self.weights["incoming_fk"] * incoming_norm[table_name]
                + self.weights["outgoing_fk"] * outgoing_norm[table_name]
                + self.weights["row_count"] * row_count_norm[table_name]
                + self.weights["keyword"] * keyword_score
            )

            scored.append({
                "table": table_name,
                "score": round(score, 4),
                "breakdown": {
                    "incoming_fk": metrics["incoming_fk"],
                    "outgoing_fk": metrics["outgoing_fk"],
                    "row_count": metrics["row_count"],
                    "keyword_match": metrics["keyword_match"],
                    "normalized": {
                        "incoming_fk": round(incoming_norm[table_name], 4),
                        "outgoing_fk": round(outgoing_norm[table_name], 4),
                        "row_count": round(row_count_norm[table_name], 4),
                        "keyword": keyword_score,
                    },
                },
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

        for i, entry in enumerate(scored, start=1):
            entry["rank"] = i

        return scored

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, Any]:
        """
        Run the full Phase 4 ranking and return everything the exporter
        needs:

            {
                "weights": {...},
                "rankings": [ {table, score, rank, breakdown}, ... ],
            }
        """

        logger.info("Starting Phase 4 importance ranking")

        rankings = self.rank_tables()

        if rankings:
            top = rankings[0]
            logger.info(
                "Most important table: '%s' (score=%.4f)",
                top["table"], top["score"],
            )

        result = {
            "weights": self.weights,
            "rankings": rankings,
        }

        logger.info("Phase 4 complete: %d table(s) ranked", len(rankings))

        return result