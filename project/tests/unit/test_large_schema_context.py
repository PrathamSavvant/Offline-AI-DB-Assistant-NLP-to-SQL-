from core.multi_stage_context_selector import MultiStageContextSelector
from core.semantic_context_builder import SemanticContextBuilder
from services.sql_reasoning_engine import PromptBuilder, QueryContextBuilder, SQLReasoningEngine, SQLValidator


class Dictionary:
    def resolve_text(self, text):
        return [{"token": "OE", "expansion": "Operating Equipment", "synonyms": ["Equipment"], "aliases": []}]


def test_multi_stage_selector_uses_dictionary_and_caps_relationship_expansion():
    tables = {name: {"name": name} for name in ("equipment", "repair_history", "inventory", "audit_log")}
    scores = {
        "equipment": {"keyword_score": 5, "combined_score": 5, "matches": ["table"]},
        "repair_history": {"keyword_score": 4, "combined_score": 4, "matches": ["tag"]},
        "inventory": {"keyword_score": 1, "combined_score": 1, "matches": ["fuzzy"]},
        "audit_log": {"keyword_score": 0, "combined_score": 0, "matches": []},
    }
    selector = MultiStageContextSelector(
        tables,
        {"repair_history": {"score": 0.8}},
        {"equipment": [{"to_table": "repair_history"}], "repair_history": [{"to_table": "equipment"}]},
        lambda name, question: scores[name],
        dictionary=Dictionary(), candidate_limit=2, neighbour_limit=1,
    )
    selected = selector.select("OE under repair", list(tables), top_n=2)
    assert [entry["table"] for entry in selected] == ["equipment", "repair_history"]


def test_semantic_context_is_metadata_based_and_marks_history_conservatively():
    schema = {"tables": [{
        "name": "repair_history", "primary_key": ["repair_id"],
        "columns": [
            {"name": "repair_id", "is_primary_key": True},
            {"name": "equipment_name"}, {"name": "repair_status"}, {"name": "created_at"},
        ],
    }]}
    builder = SemanticContextBuilder(schema, {"tables": {"repair_history": {"summary": "Stores repair events."}}})
    summary = builder.build({"question": "repairs by status", "selected_tables": ["repair_history"]})["tables"][0]
    assert summary["primary_identifier"] == ["repair_id"]
    assert summary["status_column"] == "repair_status"
    assert summary["historical"] == "likely"
    assert summary["purpose"] == "Stores repair events."


def test_semantic_context_is_rendered_before_sql_marker():
    class Generator:
        def build_context(self, question):
            return {"question": question, "selected_tables": ["equipment"], "prompt": "Schema.\n## SQL Query:\n"}

    schema = {"tables": [{"name": "equipment", "columns": [{"name": "equipment_id", "is_primary_key": True}]}]}
    context = QueryContextBuilder(Generator(), semantic_builder=SemanticContextBuilder(schema)).build("show equipment")
    prompt = PromptBuilder().build(context)
    assert "## Compact Table Semantics" in prompt
    assert prompt.index("## Compact Table Semantics") < prompt.index("## SQL Query:")


def test_disconnected_context_returns_clarification_without_calling_llm():
    class Generator:
        def build_context(self, question):
            return {
                "question": question, "selected_tables": ["equipment", "inventory"], "prompt": "Schema.",
                "join_plan": {"included_tables": ["equipment"], "unreachable_tables": ["inventory"]},
            }

    class LLM:
        def generate_structured_sql(self, prompt):
            raise AssertionError("LLM must not be called for disconnected context")

    engine = SQLReasoningEngine(QueryContextBuilder(Generator()), PromptBuilder(), SQLValidator(), lambda: None, LLM())
    result = engine.execute("show equipment inventory")
    assert result["sql"] is None
    assert "not connected" in result["final_response"]
