from core.inverted_index import InvertedTableIndex
from core.rag_layer import RAGLayer


SCHEMA = {"tables": [
    {"name": "equipments", "columns": [{"name": "id"}, {"name": "group_id"}]},
    {"name": "work_equipment", "columns": [{"name": "id"}, {"name": "status"}, {"name": "group_id"}]},
    {"name": "groups", "columns": [{"name": "id"}, {"name": "name"}]},
]}
DEFINED = {"work_equipment": {"definition": "equipment under repair and equipment collected", "synonyms": [], "abbreviations": {}}}
SEMANTICS = {"tables": {name: {"business_tags": ["equipment"]} for name in ("equipments", "work_equipment")}}


def _rag():
    index = InvertedTableIndex(); index.build(SCHEMA, semantics_data=SEMANTICS, defined_knowledge=DEFINED)
    return RAGLayer(SCHEMA, {}, SEMANTICS, {"rankings": []}, {}, inverted_index=index, defined_knowledge=DEFINED)


def test_multi_concept_coverage_beats_generic_plural_table_name():
    selected, trace = _rag().retrieve_relevant_tables("equipment waiting for collection in group", top_n=3)
    ranking = [record["table"] for record in trace.reranking]
    assert ranking.index("work_equipment") < ranking.index("equipments")
    work = next(record for record in trace.reranking if record["table"] == "work_equipment")
    assert "collect" in work["score_components"]["query_coverage_terms"]
    assert selected[0]["table"] == "work_equipment"


def test_generic_entity_query_keeps_exact_table_name_behavior():
    selected, _ = _rag().retrieve_relevant_tables("equipment", top_n=2)
    assert selected[0]["table"] == "equipments"


def test_direct_entity_is_not_polluted_by_one_generic_definition_word():
    schema = {"tables": [
        {"name": "employees", "columns": [{"name": "emp_no"}]},
        {"name": "departments", "columns": [{"name": "dept_no"}, {"name": "dept_name"}]},
    ]}
    defined = {
        "departments": {
            "definition": "Stores information about company departments and organizational details."
        }
    }
    index = InvertedTableIndex()
    index.build(schema, defined_knowledge=defined)
    rag = RAGLayer(
        schema, {}, {"tables": {}}, {"rankings": []}, {},
        inverted_index=index, defined_knowledge=defined,
    )

    selected, trace = rag.retrieve_relevant_tables(
        "how many employees in the company", top_n=3,
    )

    assert [item["table"] for item in selected] == ["employees"]
    assert any(
        item["table"] == "departments" and item["score_components"]["manual_definition_terms"] == ["company"]
        for item in trace.reranking
    )


def test_irregular_people_normalizes_to_person_semantic_term():
    schema = {"tables": [
        {"name": "opaque_01", "columns": [{"name": "opaque_id"}]},
        {"name": "departments", "columns": [{"name": "dept_no"}]},
    ]}
    semantics = {"tables": {"opaque_01": {"business_tags": ["person"]}}}
    defined = {
        "departments": {
            "definition": "Stores information about company departments."
        }
    }
    rag = RAGLayer(
        schema, {}, semantics, {"rankings": []}, {},
        defined_knowledge=defined,
    )

    selected, trace = rag.retrieve_relevant_tables(
        "how many people are there in the company", top_n=2,
    )

    assert [item["table"] for item in selected] == ["opaque_01"]
    assert "person" in trace.normalized_terms
