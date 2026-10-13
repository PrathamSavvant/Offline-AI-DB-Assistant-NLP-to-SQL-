from core.presentation_context import PresentationContextBuilder
from core.query_plan import QueryPlanBuilder
from services.sql_reasoning_engine import PromptBuilder, QueryContextBuilder


SCHEMA = {
    "tables": [
        {
            "name": "staff_records",
            "primary_key": ["staff_no"],
            "columns": [
                {"name": "staff_no", "is_primary_key": True, "datatype": "int"},
                {"name": "first_name", "datatype": "varchar"},
                {"name": "last_name", "datatype": "varchar"},
            ],
        },
        {
            "name": "pay_records",
            "primary_key": ["staff_no", "from_date"],
            "columns": [
                {"name": "staff_no", "is_primary_key": True, "datatype": "int"},
                {"name": "from_date", "is_primary_key": True, "datatype": "date"},
                {"name": "salary", "datatype": "int"},
            ],
        },
    ]
}
SEMANTICS = {
    "tables": {
        "staff_records": {"column_semantics": {"staff_no": ["identifier"], "first_name": ["descriptive"], "last_name": ["descriptive"]}},
        "pay_records": {"column_semantics": {"staff_no": ["identifier"], "from_date": ["temporal"], "salary": ["financial"]}},
    }
}


def _context(question="show salaries below 50000"):
    presentation = PresentationContextBuilder(SCHEMA, SEMANTICS).build({
        "question": question,
        "selected_tables": ["staff_records", "pay_records"],
        "join_plan": {"base_table": "staff_records"},
    })
    return {
        "question": question,
        "selected_tables": ["staff_records", "pay_records"],
        "join_plan": {"base_table": "staff_records"},
        "presentation_context": presentation,
    }


def test_entity_plan_owns_identity_display_and_requested_projection():
    plan = QueryPlanBuilder(SCHEMA).build(_context())
    assert plan.primary_entity == "staff_records"
    assert plan.result_type == "ENTITY"
    assert plan.requested_attributes == ["pay_records.salary"]
    assert plan.required_projection == [
        "staff_records.staff_no",
        "staff_records.first_name",
        "staff_records.last_name",
        "pay_records.salary",
    ]


def test_result_type_inference_distinguishes_count_aggregate_and_boolean():
    builder = QueryPlanBuilder(SCHEMA)
    assert builder.build(_context("how many staff are there")).result_type == "COUNT"
    assert builder.build(_context("average salary")).result_type == "AGGREGATE"
    assert builder.build(_context("is there any staff record")).result_type == "BOOLEAN"


def test_prompt_builder_renders_query_plan_before_sql_marker():
    class Generator:
        def build_context(self, question):
            context = _context(question)
            context["prompt"] = "Technical context.\n## SQL Query:\n"
            return context

    context = QueryContextBuilder(Generator(), query_plan_builder=QueryPlanBuilder(SCHEMA)).build("show salaries")
    prompt = PromptBuilder().build(context)
    assert "## Deterministic Query Plan" in prompt
    assert "`pay_records.salary`" in prompt
    assert prompt.index("## Deterministic Query Plan") < prompt.index("## SQL Query:")
