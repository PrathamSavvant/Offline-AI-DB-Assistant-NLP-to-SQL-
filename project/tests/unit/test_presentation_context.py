from core.presentation_context import PresentationContextBuilder
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
                {"name": "salary", "datatype": "int"},
            ],
        }
    ]
}
SEMANTICS = {
    "tables": {
        "staff_records": {
            "column_semantics": {
                "staff_no": ["identifier"],
                "first_name": ["descriptive"],
                "last_name": ["descriptive"],
                "salary": ["financial"],
            }
        }
    }
}


def test_presentation_context_infers_identity_display_and_business_columns():
    builder = PresentationContextBuilder(SCHEMA, SEMANTICS)
    presentation = builder.build({"question": "staff with salary above 90000", "selected_tables": ["staff_records"]})
    entity = presentation["entities"][0]
    assert entity["primary_entity"] is True
    assert entity["primary_key"] == ["staff_no"]
    assert entity["identity_columns"] == ["staff_no"]
    assert entity["display_columns"] == ["first_name", "last_name"]
    assert entity["important_business_columns"] == ["salary"]


def test_query_context_builder_and_prompt_builder_keep_guidance_before_sql_marker():
    class Generator:
        def build_context(self, question):
            return {
                "question": question,
                "selected_tables": ["staff_records"],
                "prompt": "Technical schema.\n## SQL Query:\n",
            }

    builder = PresentationContextBuilder(SCHEMA, SEMANTICS)
    context = QueryContextBuilder(Generator(), builder).build("show staff salary")
    prompt = PromptBuilder().build(context)
    assert "## Presentation Context (guidance)" in prompt
    assert "display: first_name, last_name" in prompt
    assert prompt.index("## Presentation Context") < prompt.index("## SQL Query:")
