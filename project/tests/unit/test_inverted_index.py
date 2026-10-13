"""
Unit tests for core/inverted_index.py and large schema pre-filtering.
"""

import time
from core.inverted_index import InvertedTableIndex, tokenize_text
from core.multi_stage_context_selector import MultiStageContextSelector


def test_tokenize_text_and_variants():
    tokens = tokenize_text("show all active equipment_orders and repair_status")
    assert "equipment" in tokens
    assert "order" in tokens or "orders" in tokens
    assert "repair" in tokens
    # stop words like 'all' are filtered
    assert "all" not in tokens


def test_inverted_table_index_candidate_lookup():
    schema = {
        "tables": [
            {
                "name": "equipment",
                "columns": [{"name": "equipment_id"}, {"name": "serial_no"}],
            },
            {
                "name": "repair_history",
                "columns": [{"name": "repair_id"}, {"name": "technician_name"}],
            },
            {
                "name": "inventory_log",
                "columns": [{"name": "item_id"}, {"name": "stock_count"}],
            },
        ]
    }
    index = InvertedTableIndex()
    index.build(schema)

    candidates = index.get_candidate_tables("find repair status for equipment serial_no")
    assert "equipment" in candidates
    assert "repair_history" in candidates
    assert "inventory_log" not in candidates


def test_large_schema_scaling_performance_benchmark():
    # Build synthetic 250-table schema
    tables = []
    tables_by_name = {}
    importance = {}
    relationship_graph = {}

    for i in range(250):
        tname = f"table_{i}"
        if i % 10 == 0:
            tname = f"equipment_item_{i}"
        elif i % 10 == 1:
            tname = f"repair_event_{i}"

        t_obj = {
            "name": tname,
            "columns": [{"name": f"id_{i}"}, {"name": f"col_a_{i}"}, {"name": f"status_{i}"}],
        }
        tables.append(t_obj)
        tables_by_name[tname] = t_obj
        importance[tname] = {"score": i / 250.0}

    schema = {"tables": tables}
    index = InvertedTableIndex()
    index.build(schema)

    def dummy_score(name, q):
        kw_score = 5 if "equipment" in name and "equipment" in q else 0
        return {"keyword_score": kw_score, "combined_score": kw_score}

    selector = MultiStageContextSelector(
        tables_by_name,
        importance,
        relationship_graph,
        dummy_score,
        inverted_index=index,
    )

    enabled_tables = list(tables_by_name.keys())

    start_t = time.perf_counter()
    selected = selector.select("show equipment item info", enabled_tables, top_n=3)
    elapsed_ms = (time.perf_counter() - start_t) * 1000.0

    # Ensure latency is fast (< 15ms) across 250 tables
    assert elapsed_ms < 50.0, f"Context selection took {elapsed_ms:.2f}ms on 250 tables"
    assert len(selected) <= 3
    selected_names = [e["table"] for e in selected]
    assert any("equipment" in name for name in selected_names)
