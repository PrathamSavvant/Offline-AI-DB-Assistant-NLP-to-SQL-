"""Critical approved semantics and joins must survive a tight schema budget."""
from core.context_generator import ContextGenerator
from core.inverted_index import InvertedTableIndex
from core.join_path_finder import JoinPathFinder
from core.relationship_builder import RelationshipBuilder
from config.settings import settings
from services.sql_reasoning_engine import PromptBuilder


def _generator(schema, knowledge):
    return ContextGenerator(schema, {}, {"tables": {}}, {"rankings": []},
                            JoinPathFinder(RelationshipBuilder(schema)),
                            defined_knowledge=knowledge)


def test_approved_column_meaning_beats_opaque_columns_in_prompt(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONTEXT_COLUMNS_PER_TABLE", 2)
    schema = {"tables": [{"name": "x_work", "columns": [
        {"name": "id", "datatype": "int", "is_primary_key": True},
        {"name": "misc_a", "datatype": "text"},
        {"name": "misc_b", "datatype": "text"},
        {"name": "x_st", "datatype": "varchar"},
    ], "foreign_keys": []}]}
    knowledge = {"x_work": {"columns": {"x_st": {"business_meaning": "equipment repair status"}}}}
    block = _generator(schema, knowledge)._render_table_block("x_work", "show equipment repair status")
    assert "x_st (varchar) — approved meaning: equipment repair status" in block
    assert "misc_a" not in block
    assert "id (int) [PRIMARY KEY]" in block


def test_composite_approved_join_keeps_every_key_in_schema_and_relationships(monkeypatch):
    monkeypatch.setattr(settings, "MAX_CONTEXT_COLUMNS_PER_TABLE", 1)
    schema = {"tables": [
        {"name": name, "columns": [{"name": col, "datatype": "int"} for col in ("unrelated", "key_a", "key_b")], "foreign_keys": []}
        for name in ("x_work", "x_group")
    ]}
    plan = {"base_table": "x_work", "sql": "FROM x_work JOIN x_group ON x_work.key_a = x_group.key_a AND x_work.key_b = x_group.key_b",
            "all_joins_verified": False, "join_edges": [{"from": "x_work", "to": "x_group",
            "from_column": "key_a", "to_column": "key_a", "from_columns": ["key_a", "key_b"],
            "to_columns": ["key_a", "key_b"], "verified": False, "provenance": "human_review"}]}
    prompt = _generator(schema, {}).build_prompt("show work by group", ["x_work", "x_group"], plan)
    assert "x_work.key_a = x_group.key_a" in prompt
    assert "x_work.key_b = x_group.key_b" in prompt
    assert prompt.count("APPROVED JOIN KEY") == 4
    assert "unrelated (int)" not in prompt


def test_approved_business_name_and_matched_alias_reach_llama_prompt():
    class Knowledge:
        def all(self):
            return {"x_work": {"business_name": "Workshop equipment movements",
                               "aliases": ["equipment movements", "unrelated inventory"],
                               "definition": "Tracks equipment entering and leaving the repair workshop."}}

    prompt = PromptBuilder().build({"question": "show equipment movements", "prompt": "## SQL Query:\n",
                                    "selected_tables": ["x_work"], "_defined_knowledge": Knowledge()})
    assert "Approved business name: Workshop equipment movements" in prompt
    assert "Matched business aliases: equipment movements" in prompt
    assert "unrelated inventory" not in prompt


def test_opaque_300_table_schema_keeps_approved_meaning_and_fk_path():
    unrelated = [{"name": f"x_{number:03d}", "columns": [{"name": "id", "datatype": "int"}], "foreign_keys": []}
                 for number in range(298)]
    work = {"name": "x_work", "columns": [
        {"name": "id", "datatype": "int", "is_primary_key": True},
        {"name": "noise_1", "datatype": "varchar"},
        {"name": "noise_2", "datatype": "varchar"},
        {"name": "group_code", "datatype": "int"},
        {"name": "x_st", "datatype": "varchar"},
    ], "foreign_keys": [{"table": "x_work", "column": "group_code", "references_table": "x_group",
                         "references_column": "id", "constraint_name": "fk_work_group", "verified": True}]}
    group = {"name": "x_group", "columns": [{"name": "id", "datatype": "int", "is_primary_key": True},
                                               {"name": "name", "datatype": "varchar"}], "foreign_keys": []}
    schema = {"tables": [*unrelated, work, group]}
    knowledge = {"x_work": {"definition": "Equipment being repaired in the workshop", "aliases": ["equipment"],
                            "columns": {"x_st": {"business_meaning": "equipment repair status"}}},
                 "x_group": {"definition": "Responsible repair group", "aliases": ["repair group"]}}
    index = InvertedTableIndex()
    index.build(schema, defined_knowledge=knowledge)
    generator = ContextGenerator(schema, {}, {"tables": {}}, {"rankings": []},
                                 JoinPathFinder(RelationshipBuilder(schema)), top_n=2,
                                 inverted_index=index, defined_knowledge=knowledge)
    context = generator.build_context("Show equipment repair status by repair group")
    assert set(context["selected_tables"]) == {"x_work", "x_group"}
    assert "x_st (varchar) — approved meaning: equipment repair status" in context["prompt"]
    assert "x_work.group_code = x_group.id" in context["prompt"]


def test_late_plan_columns_are_verified_and_shown_to_llama():
    context = {
        "question": "show current equipment",
        "selected_tables": ["x_work"],
        "tables_by_name": {"x_work": {"columns": [
            {"name": "id", "datatype": "int"},
            {"name": "x_label", "datatype": "varchar"},
            {"name": "current_flag", "datatype": "char"},
        ]}},
        "prompt": "## Database Schema\n### x_work (workshop domain)\n- id (int)\n## User Request\nshow current equipment\n## SQL Query:\n",
        "query_plan": {"status": "ready", "selected_tables": ["x_work"],
                       "required_projection": ["x_work.x_label", "x_work.fake_field"],
                       "resolved_predicates": [{"table": "x_work", "column": "current_flag", "value": "Y"}]},
    }
    prompt = PromptBuilder().build(context)
    assert "`x_work.x_label` (varchar)" in prompt
    assert "`x_work.current_flag` (char)" in prompt
    assert "fake_field" in prompt  # The plan still names it, but not as verified schema.
    physical = prompt.split("## Plan-Required Physical Columns", 1)[1].split("## Deterministic Query Plan", 1)[0]
    assert "fake_field" not in physical


def test_semantic_budget_keeps_complete_approved_facts_before_generic_summaries(monkeypatch):
    monkeypatch.setattr(settings, "SEMANTIC_CONTEXT_MAX_CHARS", 110)
    semantic = {"tables": [{"table": "x_work", "purpose": "A very long generic description " * 5}],
                "approved_predicates": [{"table": "x_work", "column": "x_st", "value": "READY"}],
                "approved_terms": [{"token": "EQ", "meaning": "equipment"}]}
    rendered = PromptBuilder._semantic_guidance(semantic)
    assert "`x_work.x_st` = 'READY'" in rendered
    assert "`EQ` means equipment" in rendered
    assert "very long generic description" not in rendered
