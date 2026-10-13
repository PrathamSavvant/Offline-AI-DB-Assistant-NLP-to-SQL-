from core.inverted_index import InvertedTableIndex
from core.rag_layer import RAGLayer
from config.settings import settings


def test_late_alphabetical_strong_table_reaches_reranker():
    tables = [{"name": f"a_{i}_equipment", "columns": []} for i in range(87)]
    tables.append({"name": "work_equipment", "columns": [{"name": "status"}]})
    schema = {"tables": tables}
    index = InvertedTableIndex()
    index.build(schema)
    rag = RAGLayer(schema, {}, {"tables": {}}, {"rankings": []}, {}, inverted_index=index)
    selected, trace = rag.retrieve_relevant_tables("work equipment", top_n=3)
    union = trace.candidate_union
    assert len(union["scored_candidates"]) <= settings.MAX_RETRIEVAL_CANDIDATES
    assert "work_equipment" in union["scored_candidates"]
    assert "work_equipment" in [item["table"] for item in trace.reranking]
    assert selected[0]["table"] == "work_equipment"
