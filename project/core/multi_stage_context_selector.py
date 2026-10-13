"""Deterministic, bounded table selection for large database schemas.

This component deliberately has no database or LLM dependency.  It narrows
lexical candidates, applies known business-dictionary expansions, adds only a
small number of directly related neighbours, then ranks the bounded set.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Set

from core.logger import get_logger

logger = get_logger(__name__)


class MultiStageContextSelector:
    """Select a small, connected, metadata-backed table context."""

    def __init__(
        self,
        tables_by_name: Dict[str, Dict[str, Any]],
        importance: Dict[str, Dict[str, Any]],
        relationship_graph: Dict[str, List[Dict[str, Any]]],
        score_table: Callable[[str, str], Dict[str, Any]],
        dictionary: Any = None,
        candidate_limit: int = 12,
        neighbour_limit: int = 2,
        inverted_index: Any = None,
    ):
        self.tables_by_name = tables_by_name
        self.importance = importance
        self.relationship_graph = relationship_graph
        self.score_table = score_table
        self.dictionary = dictionary
        self.candidate_limit = max(1, candidate_limit)
        self.neighbour_limit = max(0, neighbour_limit)
        self.inverted_index = inverted_index

    def _expanded_question(self, question: str) -> str:
        """Append only user-confirmed dictionary meanings to the query text."""
        if self.dictionary is None:
            return question
        try:
            resolved = self.dictionary.resolve_text(question)
        except Exception:
            logger.debug("Business dictionary expansion failed", exc_info=True)
            return question
        phrases = []
        for entry in resolved:
            phrases.extend([entry.get("expansion", ""), *entry.get("synonyms", []), *entry.get("aliases", [])])
        phrases = [phrase.strip() for phrase in phrases if isinstance(phrase, str) and phrase.strip()]
        return question if not phrases else question + " " + " ".join(dict.fromkeys(phrases))

    def _neighbours(self, table_name: str, enabled: Set[str]) -> Iterable[str]:
        for edge in self.relationship_graph.get(table_name, []):
            neighbour = edge.get("to_table")
            if neighbour in enabled:
                yield neighbour

    def select(self, question: str, enabled_tables: List[str], top_n: int) -> List[Dict[str, Any]]:
        """Return existing score-records in deterministic final-selection order."""
        enabled = set(enabled_tables)

        expanded_question = self._expanded_question(question)

        # Candidate pre-filtering via Inverted Index for large schemas (>20 tables)
        tables_to_score = enabled_tables
        if self.inverted_index is not None and len(enabled_tables) > 20:
            candidates = self.inverted_index.get_candidate_tables(expanded_question)
            # Always seed top central/important tables to prevent total miss
            top_central = sorted(enabled_tables, key=lambda n: -self.importance.get(n, {}).get("score", 0.0))[:5]
            candidates.update(top_central)
            candidate_set = candidates.intersection(enabled)
            if candidate_set:
                tables_to_score = list(candidate_set)
                logger.info(
                    "Context selection candidate pre-filtering: narrowed %d tables to %d candidates",
                    len(enabled_tables), len(tables_to_score),
                )

        # Stage 1 intentionally over-selects direct lexical/fuzzy candidates.
        lexical = {
            name: self.score_table(name, question.lower())
            for name in tables_to_score
        }
        lexical = {name: score for name, score in lexical.items() if score.get("keyword_score", 0) > 0}
        logger.info("Context selection stage 1 (lexical): matched %d table(s)", len(lexical))

        # Stage 2 must use only known dictionary entries.  It never invents a
        # business expansion for an abbreviation that has not been approved.
        expanded_question = self._expanded_question(question)
        expanded = {
            name: self.score_table(name, expanded_question.lower())
            for name in tables_to_score
        }
        candidates = dict(lexical)
        for name, score in expanded.items():
            if score.get("keyword_score", 0) <= 0:
                continue
            # Keep the stronger score so expansion can only add useful
            # business meaning; it must not erase a direct lexical match.
            if name not in candidates or score["combined_score"] > candidates[name]["combined_score"]:
                candidates[name] = score
        ordered = sorted(candidates, key=lambda name: (-candidates[name]["combined_score"], name))
        reduced = ordered[: self.candidate_limit]
        logger.info(
            "Context selection stage 2 (dictionary): %d candidate(s) reduced to %d",
            len(candidates), len(reduced),
        )

        # Stage 3 starts only from high-confidence seeds and adds direct graph
        # neighbours.  A hard cap prevents a hub table from exploding context.
        expanded_names = list(reduced)
        for seed in reduced[: min(top_n, len(reduced))]:
            neighbours = sorted(
                set(self._neighbours(seed, enabled)) - set(expanded_names),
                key=lambda name: (-self.importance.get(name, {}).get("score", 0.0), name),
            )[: self.neighbour_limit]
            expanded_names.extend(neighbours)
        relationship_candidates = list(dict.fromkeys(expanded_names))
        logger.info(
            "Context selection stage 3 (relationships): %d candidate(s) after capped expansion",
            len(relationship_candidates),
        )

        # Stage 4 ranks only the bounded candidate set.  Prefer a connected
        # neighbour once an anchor is selected, but never discard the highest
        # scoring candidate solely because a schema has no foreign keys.
        ranked = sorted(
            relationship_candidates,
            key=lambda name: (-candidates.get(name, expanded[name])["combined_score"], name),
        )
        selected: List[str] = []
        while ranked and len(selected) < top_n:
            connected = [name for name in ranked if not selected or any(
                name in set(self._neighbours(existing, enabled)) for existing in selected
            )]
            chosen = connected[0] if connected else ranked[0]
            selected.append(chosen)
            ranked.remove(chosen)
        logger.info("Context selection stage 4 (importance): selected %d table(s): %s", len(selected), selected)

        return [
            {"table": name, **candidates.get(name, expanded[name])}
            for name in selected
        ]
