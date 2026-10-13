from core.inverted_index import InvertedTableIndex
from core.rag_layer import RAGLayer


def _table(name, columns=None):
    return {"name": name, "columns": [{"name": c} for c in (columns or [f"{name}_id"])]}


def _rag(schema, graph=None, defined=None):
    return RAGLayer(
        schema={"tables": schema},
        grouping_data={}, semantics_data={"tables": {}}, ranking_data={"rankings": []},
        relationship_graph=graph or {}, defined_knowledge=defined or {},
    )


def test_manual_definition_synonym_and_abbreviation_are_retrieval_signals():
    schema = [_table("work_equipment", ["work_equipment_id", "repair_class", "group_id"]), _table("users")]
    defined = {"work_equipment": {
        "definition": "Equipment used in work orders and maintenance.",
        "synonyms": ["maintenance equipment"],
        "abbreviations": {"OE": {"meaning": "Operating Equipment"}},
    }}
    rag = _rag(schema, defined=defined)
    selected, trace = rag.retrieve_relevant_tables("show OE under repair", top_n=2)
    assert selected[0]["table"] == "work_equipment"
    assert "oe" in trace.normalized_terms


def test_connected_audit_table_is_not_added_without_retrieval_signal():
    schema = [_table("allergy", ["allergy_id", "patient_id"]), _table("patient", ["patient_id"]), _table("users", ["user_id"])]
    graph = {"allergy": [{"to_table": "patient"}, {"to_table": "users"}], "patient": [{"to_table": "allergy"}], "users": [{"to_table": "allergy"}]}
    selected, trace = _rag(schema, graph).retrieve_relevant_tables("how many people with allergy", top_n=2)
    assert "users" not in [item["table"] for item in selected]
    assert trace.relationship_expansion["neighbors_added"] == {}


def test_manual_index_update_reindexes_only_the_changed_table_terms():
    index = InvertedTableIndex()
    schema = {"tables": [_table("work_equipment"), _table("groups")]}
    index.build(schema)
    index.update_manual_knowledge("work_equipment", {"synonyms": ["operating equipment"], "abbreviations": {"OE": {"meaning": "Operating Equipment"}}})
    assert index.get_candidate_tables("OE repairs") == {"work_equipment"}
    index.update_manual_knowledge("work_equipment", {"synonyms": ["assets"], "abbreviations": {}})
    assert "work_equipment" not in index.get_candidate_tables("OE")
    assert index.get_candidate_tables("assets") == {"work_equipment"}


def test_large_schema_uses_bounded_candidate_set():
    schema = [_table("work_equipment", ["work_equipment_id", "repair_class"])]
    schema.extend(_table(f"audit_event_{number}", ["id", "created_at", "status"]) for number in range(500))
    index = InvertedTableIndex()
    schema_doc = {"tables": schema}
    index.build(schema_doc)
    rag = RAGLayer(schema_doc, {}, {"tables": {}}, {"rankings": []}, {}, inverted_index=index)
    selected, trace = rag.retrieve_relevant_tables("work equipment repair", top_n=4)
    assert [item["table"] for item in selected] == ["work_equipment"]
    assert trace.lexical_retrieval["subset_evaluated"] < 10
    assert trace.timings_ms["total"] >= 0
