"""
core/fuzzy_search.py

Fuzzy Search — typo/variant-tolerant matching over table and column names,
using RapidFuzz.

Requirement #7 of the extension: things like

    userid, uesr id, uber id, user-id, usr id

should all resolve to the same underlying "user_id" style column, even
though none of them match with a plain substring/whole-word check.

This module runs as one signal inside the Phase 9 Hybrid Retrieval
(core/context_generator.py) — it never replaces the existing keyword
scoring, it only adds a bonus when a fuzzy match clears a confidence
threshold.

RapidFuzz is an optional dependency: if it isn't installed, this module
degrades to returning no matches rather than raising, so the rest of the
pipeline (which must keep working exactly as before) is unaffected.
"""

from typing import Any, Dict, List, Tuple

from core.logger import get_logger

logger = get_logger(__name__)

try:
    from rapidfuzz import fuzz, process
    RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when dependency missing
    RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not installed — fuzzy search signal will be disabled")

# A match below this score (0-100) is considered noise and ignored.
DEFAULT_SCORE_CUTOFF = 75


class FuzzySearch:
    """
    Usage:
        fuzzy = FuzzySearch(schema)
        fuzzy.match_tables("uesr")         -> [("users", 82.0), ...]
        fuzzy.match_columns("user id")     -> [(("users", "user_id"), 95.0), ...]
    """

    def __init__(self, schema: Dict[str, Any], score_cutoff: int = DEFAULT_SCORE_CUTOFF):
        self.score_cutoff = score_cutoff
        self.table_names: List[str] = [t["name"] for t in schema.get("tables", [])]

        self.column_index: List[Tuple[str, str]] = []
        self.column_labels: List[str] = []
        for table in schema.get("tables", []):
            for column in table.get("columns", []):
                self.column_index.append((table["name"], column["name"]))
                self.column_labels.append(f"{table['name']} {column['name']}".replace("_", " "))

        self.table_labels = [name.replace("_", " ") for name in self.table_names]

    @staticmethod
    def _normalize(text: str) -> str:
        return text.replace("-", " ").replace("_", " ").strip().lower()

    def match_tables(self, query: str, limit: int = 5) -> List[Tuple[str, float]]:
        if not RAPIDFUZZ_AVAILABLE or not query.strip() or not self.table_names:
            return []

        query_norm = self._normalize(query)
        results = process.extract(
            query_norm, self.table_labels, scorer=fuzz.WRatio,
            limit=limit, score_cutoff=self.score_cutoff,
        )
        return [(self.table_names[idx], score) for _, score, idx in results]

    def match_columns(self, query: str, limit: int = 8) -> List[Tuple[Tuple[str, str], float]]:
        if not RAPIDFUZZ_AVAILABLE or not query.strip() or not self.column_index:
            return []

        query_norm = self._normalize(query)
        results = process.extract(
            query_norm, self.column_labels, scorer=fuzz.WRatio,
            limit=limit, score_cutoff=self.score_cutoff,
        )
        return [(self.column_index[idx], score) for _, score, idx in results]

    def score_for_table(self, question: str, table_name: str) -> float:
        """
        Best fuzzy match score (0-1, normalized) between the question and
        either the table name itself or any of its columns — used as one
        input into the Phase 9 hybrid relevance score.
        """

        if not RAPIDFUZZ_AVAILABLE:
            return 0.0

        best = 0.0
        table_label = table_name.replace("_", " ")
        best = max(best, fuzz.partial_ratio(self._normalize(question), table_label))

        for t_name, c_name in self.column_index:
            if t_name != table_name:
                continue
            label = c_name.replace("_", " ")
            best = max(best, fuzz.partial_ratio(self._normalize(question), label) * 0.9)

        return best / 100.0 if best >= self.score_cutoff else 0.0
