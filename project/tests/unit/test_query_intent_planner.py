from core.query_intent_planner import QueryIntentPlanner
from services.sql_reasoning_engine import PromptBuilder, QueryContextBuilder


SCHEMA = {
    "tables": [
        {"name": "groups", "columns": [{"name": "id"}, {"name": "name"}, {"name": "status"}]},
        {"name": "work_equipment", "columns": [
            {"name": "id"}, {"name": "group_id"}, {"name": "eqpt"},
            {"name": "repair_class"}, {"name": "repair_remarks"},
        ]},
        {"name": "salaries", "columns": [{"name": "employee_id"}, {"name": "salary", "datatype": "decimal"}]},
        {"name": "employees", "columns": [{"name": "id"}, {"name": "name"}]},
    ]
}
SEMANTICS = {"tables": {"work_equipment": {"business_tags": ["repair"]}}}
PRESENTATION = {
    "entities": [
        {"table": "groups", "identity_columns": ["id"], "display_columns": ["name"]},
        {"table": "work_equipment", "identity_columns": ["id"], "display_columns": ["eqpt"]},
        {"table": "salaries", "identity_columns": ["employee_id"], "display_columns": []},
        {"table": "employees", "identity_columns": ["id"], "display_columns": ["name"]},
    ]
}


def _context(question, selected, joins=None):
    return {
        "question": question,
        "selected_tables": selected,
        "presentation_context": PRESENTATION,
        "join_plan": {"join_edges": joins or []},
    }


def test_primary_entity_distinguishes_returned_equipment_from_group_lookup():
    planner = QueryIntentPlanner(SCHEMA, SEMANTICS)
    intent = planner.build(_context(
        "show equipment under repair in OE group",
        ["groups", "work_equipment"],
        [{"from": "groups", "from_column": "id", "to": "work_equipment", "to_column": "group_id"}],
    ))
    assert intent.primary_entity == "work_equipment"
    assert "work_equipment.id" in intent.projection
    assert "work_equipment.repair_class" in intent.projection
    assert intent.joins == ["groups.id = work_equipment.group_id"]


def test_planner_resolves_unambiguous_lookup_and_categorical_filters():
    planner = QueryIntentPlanner(SCHEMA, SEMANTICS)
    intent = planner.build(_context("show equipment under repair in OE group", ["groups", "work_equipment"]))
    filters = {(item.table, item.column, item.value) for item in intent.filters}
    assert ("groups", "name", "OE") in filters
    assert ("work_equipment", "repair_class", "Repair") in filters


def test_numeric_filter_and_aggregation_are_planned_without_sql_generation():
    planner = QueryIntentPlanner(SCHEMA)
    intent = planner.build(_context("average salary above 90000", ["salaries"]))
    assert intent.result_type == "AGGREGATE"
    assert any(item.column == "salary" and item.operator == ">" and item.value == 90000 for item in intent.filters)


def test_dictionary_expansion_contributes_to_entity_resolution():
    class Dictionary:
        def resolve_text(self, question):
            return [{"expansion": "employee", "synonyms": [], "aliases": []}]

    planner = QueryIntentPlanner(SCHEMA, dictionary=Dictionary())
    assert planner.build(_context("list emp", ["employees"])).primary_entity == "employees"


def test_intent_is_rendered_before_sql_marker_without_changing_builder_interface():
    class Generator:
        def build_context(self, question):
            context = _context(question, ["groups", "work_equipment"])
            context["prompt"] = "Schema context.\n## SQL Query:\n"
            return context

    planner = QueryIntentPlanner(SCHEMA, SEMANTICS)
    context = QueryContextBuilder(Generator(), intent_planner=planner).build("show equipment under repair in OE group")
    prompt = PromptBuilder().build(context)
    assert "## Resolved Business Intent" in prompt
    assert "Primary output entity: `work_equipment`" in prompt
    assert prompt.index("## Resolved Business Intent") < prompt.index("## SQL Query:")
