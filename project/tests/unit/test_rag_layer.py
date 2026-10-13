"""
Unit tests for core/rag_layer.py

Covers all 4 pipeline stages:
  1. MetadataFilter  — enabled/disabled table filtering, priority weights
  2. LexicalRetriever — token/column/annotation/fuzzy/defined-knowledge matches
  3. VectorRetriever  — embedding hit delegation and offline fallback
  4. Reranker         — multi-signal fusion and FK relationship expansion
  5. RAGLayer         — end-to-end retrieval and RetrievalTrace
"""

import pytest
from unittest.mock import MagicMock

from core.rag_layer import (
    MetadataFilter,
    LexicalRetriever,
    VectorRetriever,
    Reranker,
    RAGLayer,
    RetrievalTrace,
    SCORE_WEIGHTS,
    PRIORITY_WEIGHTS,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_table(name: str, columns: list[str] | None = None) -> dict:
    cols = [{"name": c} for c in (columns or [])]
    return {"name": name, "columns": cols}


SCHEMA = {
    "tables": [
        _make_table("employees", ["emp_id", "first_name", "last_name", "department_id", "salary"]),
        _make_table("departments", ["dept_id", "dept_name", "manager_id"]),
        _make_table("salaries", ["emp_id", "amount", "effective_date"]),
        _make_table("audit_log", ["log_id", "created_at", "updated_at"]),
    ]
}

GROUPING = {
    "table_to_domain": {
        "employees": "Human Resources",
        "departments": "Human Resources",
        "salaries": "Payroll",
        "audit_log": "Audit",
    }
}

SEMANTICS = {
    "tables": {
        "employees": {"business_tags": ["staff", "headcount", "workforce"]},
        "departments": {"business_tags": ["org", "division"]},
        "salaries": {"business_tags": ["pay", "compensation"]},
        "audit_log": {"business_tags": ["audit", "log"]},
    }
}

RANKING = {
    "rankings": [
        {"table": "employees", "score": 0.9, "rank": 1},
        {"table": "departments", "score": 0.7, "rank": 2},
        {"table": "salaries", "score": 0.6, "rank": 3},
        {"table": "audit_log", "score": 0.1, "rank": 4},
    ]
}

ANNOTATIONS = {
    "employees": {"enabled": True, "priority": "High", "aliases": ["staff", "workers"], "meaning": ""},
    "departments": {"enabled": True, "priority": "Medium", "aliases": [], "meaning": ""},
    "salaries": {"enabled": True, "priority": "Medium", "aliases": ["pay records"], "meaning": ""},
    "audit_log": {"enabled": False, "priority": "Low", "aliases": [], "meaning": ""},
}

RELATIONSHIP_GRAPH = {
    "employees": [{"to_table": "departments"}, {"to_table": "salaries"}],
    "departments": [{"to_table": "employees"}],
    "salaries": [{"to_table": "employees"}],
    "audit_log": [],
}


# ===========================================================================
# Stage 1 — MetadataFilter
# ===========================================================================

class TestMetadataFilter:
    def test_enabled_true_by_default(self):
        mf = MetadataFilter({})
        assert mf.is_enabled("any_table") is True

    def test_explicitly_disabled(self):
        mf = MetadataFilter({"secret_table": {"enabled": False}})
        assert mf.is_enabled("secret_table") is False

    def test_filter_tables_splits_correctly(self):
        mf = MetadataFilter(ANNOTATIONS)
        enabled, disabled = mf.filter_tables(list(ANNOTATIONS))
        assert set(enabled) == {"employees", "departments", "salaries"}
        assert disabled == ["audit_log"]

    def test_priority_weight_critical(self):
        mf = MetadataFilter({"t": {"priority": "Critical"}})
        assert mf.get_priority_weight("t") == PRIORITY_WEIGHTS["Critical"]

    def test_priority_weight_defaults_to_medium(self):
        mf = MetadataFilter({})
        assert mf.get_priority_weight("unknown") == PRIORITY_WEIGHTS["Medium"]

    def test_priority_weight_low(self):
        mf = MetadataFilter({"t": {"priority": "Low"}})
        assert mf.get_priority_weight("t") == PRIORITY_WEIGHTS["Low"]


# ===========================================================================
# Stage 2 — LexicalRetriever
# ===========================================================================

class TestLexicalRetriever:
    def _make_retriever(self, **kwargs) -> LexicalRetriever:
        tables_by_name = {t["name"]: t for t in SCHEMA["tables"]}
        return LexicalRetriever(
            tables_by_name=tables_by_name,
            table_to_domain=GROUPING["table_to_domain"],
            semantics=SEMANTICS["tables"],
            annotations=ANNOTATIONS,
            **kwargs,
        )

    # ---- table name matching -------------------------------------------

    def test_table_name_exact_match(self):
        lr = self._make_retriever()
        result = lr.score_table("employees", "show all employees")
        assert result["score"] >= SCORE_WEIGHTS["table_name_match"]
        assert any("table_name" in m for m in result["matches"])

    def test_table_name_plural_variant(self):
        """'department' should match 'departments' table."""
        lr = self._make_retriever()
        result = lr.score_table("departments", "list each department")
        assert result["score"] > 0

    def test_no_match_returns_zero(self):
        lr = self._make_retriever()
        result = lr.score_table("audit_log", "show employee salaries")
        assert result["score"] == 0.0

    # ---- column matching -----------------------------------------------

    def test_column_name_match(self):
        lr = self._make_retriever()
        result = lr.score_table("employees", "what is the salary for each employee")
        # 'salary' is a column in employees; 'salary' also hits salaries table_name
        # just verify employees scores > 0 from the column hit
        assert result["score"] > 0

    def test_generic_column_ignored(self):
        """created_at/updated_at should never contribute a match."""
        lr = self._make_retriever()
        result = lr.score_table("audit_log", "show created_at and updated_at")
        # These are generic columns — must not fire a column_match signal
        col_matches = [m for m in result["matches"] if m.startswith("column:")]
        assert len(col_matches) == 0

    # ---- annotation alias matching ------------------------------------

    def test_annotation_alias_match(self):
        lr = self._make_retriever()
        result = lr.score_table("employees", "how many workers are in each department")
        assert any("alias:workers" in m for m in result["matches"])
        assert result["score"] >= SCORE_WEIGHTS["annotation_alias_match"]

    # ---- business-tag matching ----------------------------------------

    def test_business_tag_match(self):
        lr = self._make_retriever()
        result = lr.score_table("employees", "total headcount by location")
        assert any("tag:headcount" in m for m in result["matches"])

    # ---- defined knowledge synonyms -----------------------------------

    def test_defined_knowledge_synonym_match(self):
        lr = self._make_retriever(
            defined_knowledge={
                "salaries": {"synonyms": ["remuneration", "wage"], "abbreviations": {}}
            }
        )
        result = lr.score_table("salaries", "what is the remuneration for Q1")
        assert any("defined_synonym:remuneration" in m for m in result["matches"])
        assert result["score"] >= SCORE_WEIGHTS["annotation_alias_match"]

    def test_defined_knowledge_abbreviation_match(self):
        lr = self._make_retriever(
            defined_knowledge={
                "salaries": {
                    "synonyms": [],
                    "abbreviations": {"SAL": {"meaning": "salary amount", "abbreviation": "SAL"}},
                }
            }
        )
        result = lr.score_table("salaries", "show SAL breakdown")
        assert any("defined_abbr:SAL" in m for m in result["matches"])

    # ---- inverted index candidate subsetting --------------------------

    def test_inverted_index_prefilter_small_schema(self):
        """Schema with ≤20 tables should return all tables unfiltered."""
        lr = self._make_retriever()
        enabled = ["employees", "departments", "salaries"]
        candidates = lr.get_candidate_subset("show employees", enabled)
        assert set(candidates) == set(enabled)

    def test_inverted_index_prefilter_large_schema(self):
        """Schema >20 tables should use index to narrow candidates."""
        mock_index = MagicMock()
        mock_index.get_candidate_tables.return_value = {"employees", "salaries"}

        tables_by_name = {t["name"]: t for t in SCHEMA["tables"]}
        # Simulate 21 enabled tables
        enabled = [f"table_{i}" for i in range(21)]
        lr = LexicalRetriever(
            tables_by_name=tables_by_name,
            table_to_domain={},
            semantics={},
            inverted_index=mock_index,
        )
        candidates = lr.get_candidate_subset("employee salaries", enabled)
        mock_index.get_candidate_tables.assert_called_once()
        # Should return the intersection — but since fake enabled has no overlap,
        # fallback to full enabled list
        assert isinstance(candidates, list)

    # ---- dictionary expansion ----------------------------------------

    def test_dictionary_expansion_applied(self):
        mock_dict = MagicMock()
        mock_dict.resolve_text.return_value = [
            {"expansion": "human resources", "synonyms": [], "aliases": [], "token": "HR"}
        ]
        mock_dict.lookup.return_value = []

        lr = self._make_retriever(dictionary=mock_dict)
        expanded = lr.expand_question("show HR data")
        assert "human resources" in expanded


# ===========================================================================
# Stage 3 — VectorRetriever
# ===========================================================================

class TestVectorRetriever:
    def test_returns_empty_dict_when_no_embedding_search(self):
        vr = VectorRetriever(embedding_search=None)
        assert vr.search("anything") == {}

    def test_delegates_to_embedding_search(self):
        mock_es = MagicMock()
        mock_es.search.return_value = [("employees", 0.92), ("departments", 0.78)]
        vr = VectorRetriever(embedding_search=mock_es)
        scores = vr.search("show all employees", top_k=5)
        mock_es.search.assert_called_once_with("show all employees", top_k=5)
        assert scores["employees"] == pytest.approx(0.92)
        assert scores["departments"] == pytest.approx(0.78)

    def test_filters_zero_scores(self):
        mock_es = MagicMock()
        mock_es.search.return_value = [("employees", 0.85), ("audit_log", 0.0)]
        vr = VectorRetriever(embedding_search=mock_es)
        scores = vr.search("employee info")
        assert "employees" in scores
        assert "audit_log" not in scores

    def test_graceful_fallback_on_exception(self):
        mock_es = MagicMock()
        mock_es.search.side_effect = RuntimeError("FAISS unavailable")
        vr = VectorRetriever(embedding_search=mock_es)
        scores = vr.search("anything")
        assert scores == {}


# ===========================================================================
# Stage 4 — Reranker
# ===========================================================================

class TestReranker:
    def _make_reranker(self, **kwargs) -> Reranker:
        importance = {
            "employees": {"score": 0.9, "rank": 1},
            "departments": {"score": 0.7, "rank": 2},
            "salaries": {"score": 0.6, "rank": 3},
            "audit_log": {"score": 0.1, "rank": 4},
        }
        return Reranker(
            importance=importance,
            relationship_graph=RELATIONSHIP_GRAPH,
            candidate_limit=kwargs.pop("candidate_limit", 12),
            neighbour_limit=kwargs.pop("neighbour_limit", 2),
        )

    def _make_mf(self) -> MetadataFilter:
        return MetadataFilter(ANNOTATIONS)

    def test_only_tables_with_signal_are_selected(self):
        rr = self._make_reranker()
        mf = self._make_mf()
        lexical = {
            "employees": {"score": 5.0, "matches": ["table_name (+5.0)"]},
            "departments": {"score": 0.0, "matches": []},
            "salaries": {"score": 0.0, "matches": []},
            "audit_log": {"score": 0.0, "matches": []},
        }
        selected, candidates, _ = rr.rerank(
            ["employees", "departments", "salaries", "audit_log"],
            lexical, {}, mf, top_n=3,
        )
        selected_names = [s["table"] for s in selected]
        assert "employees" in selected_names

    def test_vector_score_contributes_to_ranking(self):
        rr = self._make_reranker()
        mf = self._make_mf()
        lexical = {
            "employees": {"score": 0.0, "matches": []},
            "salaries":  {"score": 0.0, "matches": []},
            "departments": {"score": 0.0, "matches": []},
            "audit_log": {"score": 0.0, "matches": []},
        }
        vector = {"salaries": 0.8}  # only salaries has a vector hit
        selected, _, _ = rr.rerank(
            ["employees", "salaries", "departments", "audit_log"],
            lexical, vector, mf, top_n=1,
        )
        # Vector similarity is useful for ranking, but cannot ground a table
        # without deterministic schema or approved-knowledge evidence.
        assert selected == []

    def test_fk_expansion_only_after_seeds(self):
        """FK neighbours should only be added for seed tables, not pre-filter stage."""
        rr = self._make_reranker(neighbour_limit=1)
        mf = self._make_mf()
        # Only employees has signal
        lexical = {
            "employees": {"score": 7.0, "matches": ["table_name (+5.0)", "alias:workers (+6.0)"]},
            "departments": {"score": 0.0, "matches": []},
            "salaries": {"score": 0.0, "matches": []},
            "audit_log": {"score": 0.0, "matches": []},
        }
        selected, _, rel_info = rr.rerank(
            ["employees", "departments", "salaries", "audit_log"],
            lexical, {}, mf, top_n=2,
        )
        # employees is a seed; at most 1 FK neighbour should be added
        assert "employees" in rel_info["seeds"]
        neighbors_added = rel_info["neighbors_added"].get("employees", [])
        assert len(neighbors_added) <= 1

    def test_no_fallback_when_no_signal(self):
        """Priority/importance must never fill top-k without query evidence."""
        rr = self._make_reranker()
        mf = self._make_mf()
        lexical = {t: {"score": 0.0, "matches": []} for t in ["employees", "departments", "salaries", "audit_log"]}
        selected, _, _ = rr.rerank(
            ["employees", "departments", "salaries", "audit_log"],
            lexical, {}, mf, top_n=2,
        )
        assert selected == []

    def test_disabled_table_excluded_from_expansion(self):
        """audit_log (disabled) should not appear as an FK neighbour."""
        graph = {
            "employees": [{"to_table": "audit_log"}],
        }
        importance = {
            "employees": {"score": 0.9},
            "audit_log": {"score": 0.5},
        }
        rr = Reranker(importance=importance, relationship_graph=graph, neighbour_limit=2)
        mf = MetadataFilter({"audit_log": {"enabled": False}})
        lexical = {"employees": {"score": 5.0, "matches": ["table_name (+5.0)"]}}
        # Only pass enabled tables to the reranker
        selected, _, rel_info = rr.rerank(
            ["employees"],  # audit_log NOT in enabled set
            lexical, {}, mf, top_n=2,
        )
        # audit_log must not appear in selected or neighbours
        selected_names = {s["table"] for s in selected}
        all_neighbours = [n for ns in rel_info["neighbors_added"].values() for n in ns]
        assert "audit_log" not in selected_names
        assert "audit_log" not in all_neighbours


# ===========================================================================
# Stage 5 — RAGLayer (composite, end-to-end)
# ===========================================================================

class TestRAGLayer:
    def _make_rag(self, **kwargs) -> RAGLayer:
        return RAGLayer(
            schema=SCHEMA,
            grouping_data=GROUPING,
            semantics_data=SEMANTICS,
            ranking_data=RANKING,
            relationship_graph=RELATIONSHIP_GRAPH,
            annotations=ANNOTATIONS,
            **kwargs,
        )

    def test_returns_selected_tables_and_trace(self):
        rag = self._make_rag()
        selected, trace = rag.retrieve_relevant_tables("list all employees", top_n=2)
        assert isinstance(selected, list)
        assert isinstance(trace, RetrievalTrace)
        assert trace.question == "list all employees"

    def test_disabled_table_never_returned(self):
        rag = self._make_rag()
        selected, trace = rag.retrieve_relevant_tables("show audit_log entries", top_n=3)
        selected_names = [s["table"] for s in selected]
        assert "audit_log" not in selected_names
        assert "audit_log" in trace.metadata_filtering["disabled_tables"]

    def test_retrieval_trace_has_all_stages(self):
        rag = self._make_rag()
        _, trace = rag.retrieve_relevant_tables("employee salary info", top_n=2)
        d = trace.to_dict()
        assert "metadata_filtering" in d
        assert "lexical_retrieval" in d
        assert "candidate_union" in d
        assert "vector_retrieval" in d
        assert "reranking" in d
        assert "relationship_expansion" in d
        assert "selected_tables" in d

    def test_all_tables_disabled_returns_empty(self):
        all_disabled = {name: {"enabled": False} for name in ["employees", "departments", "salaries", "audit_log"]}
        rag = RAGLayer(
            schema=SCHEMA,
            grouping_data=GROUPING,
            semantics_data=SEMANTICS,
            ranking_data=RANKING,
            relationship_graph=RELATIONSHIP_GRAPH,
            annotations=all_disabled,
        )
        selected, trace = rag.retrieve_relevant_tables("show employees", top_n=3)
        assert selected == []
        assert trace.selected_tables == []

    @pytest.mark.parametrize("question", ["hello", "random nonsense text", "tell me something"])
    def test_ungrounded_text_returns_zero_tables(self, question):
        selected, trace = self._make_rag().retrieve_relevant_tables(question, top_n=3)
        assert selected == []
        assert trace.selected_tables == []

    def test_vector_retriever_used_when_provided(self):
        mock_es = MagicMock()
        mock_es.search.return_value = [("salaries", 0.91)]
        rag = self._make_rag(embedding_search=mock_es)
        selected, trace = rag.retrieve_relevant_tables("what is the pay scale", top_n=2)
        mock_es.search.assert_called_once()
        assert trace.vector_retrieval["enabled"] is True
        assert trace.vector_retrieval["hits_count"] == 1

    def test_no_vector_retriever_trace_shows_disabled(self):
        rag = self._make_rag()
        _, trace = rag.retrieve_relevant_tables("employee count", top_n=2)
        assert trace.vector_retrieval["enabled"] is False

    def test_lexical_trace_includes_matched_tables(self):
        rag = self._make_rag()
        _, trace = rag.retrieve_relevant_tables("list all employees", top_n=2)
        matched_names = [e["table"] for e in trace.lexical_retrieval.get("top_lexical", [])]
        assert "employees" in matched_names

    def test_selected_tables_in_trace_match_returned_list(self):
        rag = self._make_rag()
        selected, trace = rag.retrieve_relevant_tables("show department heads", top_n=2)
        assert trace.selected_tables == [s["table"] for s in selected]

    def test_defined_knowledge_passed_through_to_lexical(self):
        """Defined knowledge synonyms must score on lexical retrieval."""
        defined_knowledge = {
            "salaries": {"synonyms": ["remuneration"], "abbreviations": {}}
        }
        rag = self._make_rag(defined_knowledge=defined_knowledge)
        selected, trace = rag.retrieve_relevant_tables(
            "show remuneration breakdown", top_n=3
        )
        selected_names = [s["table"] for s in selected]
        assert "salaries" in selected_names

    def test_fk_relationships_only_used_after_retrieval(self):
        """Relationship expansion info in trace must reference seed tables from the retrieval step."""
        rag = self._make_rag()
        _, trace = rag.retrieve_relevant_tables("employee department data", top_n=2)
        seeds = trace.relationship_expansion.get("seeds", [])
        # Seeds must be a subset of actually-selected tables
        assert set(seeds).issubset(set(trace.selected_tables) | set(
            n for ns in trace.relationship_expansion.get("neighbors_added", {}).values() for n in ns
        ) | set(trace.selected_tables))


# ===========================================================================
# RetrievalTrace serialisation
# ===========================================================================

class TestRetrievalTrace:
    def test_to_dict_is_json_serialisable(self):
        import json
        trace = RetrievalTrace(
            question="test",
            metadata_filtering={"total_tables": 4, "enabled_count": 3, "disabled_tables": ["audit_log"]},
            lexical_retrieval={"subset_evaluated": 3, "matched_count": 1, "top_lexical": []},
            vector_retrieval={"enabled": False, "hits_count": 0, "scores": {}},
            reranking=[],
            relationship_expansion={"seeds": ["employees"], "neighbors_added": {}},
            selected_tables=["employees"],
        )
        d = trace.to_dict()
        # Must be JSON-serialisable (no sets, no custom objects)
        serialised = json.dumps(d)
        assert "employees" in serialised
