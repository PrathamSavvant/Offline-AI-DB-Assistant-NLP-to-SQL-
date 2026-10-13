"""Unit tests for Phase 16 Database Reliability Hardening.

Covers:
  - Misleading table-name matches & bystander pruning
  - Entity vs Literal Filter Disambiguation
  - Join Provenance & Path Validation
  - Column & Table Schema Validation in SQLValidator
  - Pending Knowledge Isolation
  - Abbreviation Grounding & Single-Table Sufficiency
"""

import pytest
from core.context_generator import ContextGenerator
from core.join_path_finder import JoinPathFinder
from core.predicate_planner import PredicatePlanner
from core.query_intent_planner import QueryIntentPlanner
from core.rag_layer import RAGLayer, Reranker, MetadataFilter
from core.relationship_builder import RelationshipBuilder
from services.sql_reasoning_engine import SQLValidator, SQLValidationError


@pytest.fixture
def employees_schema():
    return {
        "database": "employees",
        "tables": [
            {
                "name": "employees",
                "columns": [
                    {"name": "emp_no", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "birth_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                    {"name": "first_name", "datatype": "varchar(14)", "is_primary_key": False, "nullable": False},
                    {"name": "last_name", "datatype": "varchar(16)", "is_primary_key": False, "nullable": False},
                    {"name": "gender", "datatype": "enum('M','F')", "is_primary_key": False, "nullable": False},
                    {"name": "hire_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no"],
                "foreign_keys": [],
            },
            {
                "name": "departments",
                "columns": [
                    {"name": "dept_no", "datatype": "char(4)", "is_primary_key": True, "nullable": False},
                    {"name": "dept_name", "datatype": "varchar(40)", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["dept_no"],
                "foreign_keys": [],
            },
            {
                "name": "dept_manager",
                "columns": [
                    {"name": "emp_no", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "dept_no", "datatype": "char(4)", "is_primary_key": True, "nullable": False},
                    {"name": "from_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                    {"name": "to_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "dept_no"],
                "foreign_keys": [
                    {"table": "dept_manager", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_dm_emp"},
                    {"table": "dept_manager", "column": "dept_no", "references_table": "departments", "references_column": "dept_no", "constraint_name": "fk_dm_dept"},
                ],
            },
            {
                "name": "dept_emp",
                "columns": [
                    {"name": "emp_no", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "dept_no", "datatype": "char(4)", "is_primary_key": True, "nullable": False},
                    {"name": "from_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                    {"name": "to_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "dept_no"],
                "foreign_keys": [
                    {"table": "dept_emp", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_de_emp"},
                    {"table": "dept_emp", "column": "dept_no", "references_table": "departments", "references_column": "dept_no", "constraint_name": "fk_de_dept"},
                ],
            },
            {
                "name": "salaries",
                "columns": [
                    {"name": "emp_no", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "salary", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "from_date", "datatype": "date", "is_primary_key": True, "nullable": False},
                    {"name": "to_date", "datatype": "date", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "from_date"],
                "foreign_keys": [
                    {"table": "salaries", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_sal_emp"},
                ],
            },
            {
                "name": "titles",
                "columns": [
                    {"name": "emp_no", "datatype": "int", "is_primary_key": True, "nullable": False},
                    {"name": "title", "datatype": "varchar(50)", "is_primary_key": True, "nullable": False},
                    {"name": "from_date", "datatype": "date", "is_primary_key": True, "nullable": False},
                    {"name": "to_date", "datatype": "date", "is_primary_key": False, "nullable": True},
                ],
                "primary_key": ["emp_no", "title", "from_date"],
                "foreign_keys": [
                    {"table": "titles", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_tit_emp"},
                ],
            },
        ],
        "relationships": [
            {"from_table": "dept_manager", "via_column": "emp_no", "to_table": "employees", "to_column": "emp_no"},
            {"from_table": "dept_manager", "via_column": "dept_no", "to_table": "departments", "to_column": "dept_no"},
            {"from_table": "dept_emp", "via_column": "emp_no", "to_table": "employees", "to_column": "emp_no"},
            {"from_table": "dept_emp", "via_column": "dept_no", "to_table": "departments", "to_column": "dept_no"},
            {"from_table": "salaries", "via_column": "emp_no", "to_table": "employees", "to_column": "emp_no"},
            {"from_table": "titles", "via_column": "emp_no", "to_table": "employees", "to_column": "emp_no"},
        ],
    }


def test_bystander_table_pruning_on_single_entity_query(employees_schema):
    """Test that a single-entity query with multiple column matches does not pull in bystander tables."""
    rel_builder = RelationshipBuilder(employees_schema)
    join_finder = JoinPathFinder(rel_builder)
    grouping_data = {"table_to_domain": {t["name"]: "Operations" for t in employees_schema["tables"]}}
    semantics_data = {"tables": {}}
    ranking_data = {"rankings": [{"table": t["name"], "score": 1.0, "rank": i} for i, t in enumerate(employees_schema["tables"])]}

    generator = ContextGenerator(
        schema=employees_schema,
        grouping_data=grouping_data,
        semantics_data=semantics_data,
        ranking_data=ranking_data,
        join_finder=join_finder,
        top_n=3,
    )

    # "Show the date of birth and gender of employee 10001" only targets `employees`
    context = generator.build_context("Show the date of birth and gender of employee 10001")
    assert context["selected_tables"] == ["employees"], f"Expected only ['employees'], got {context['selected_tables']}"


def test_entity_disambiguation_does_not_create_bogus_literal_filter(employees_schema):
    """Test that 'Show all department managers' does NOT turn 'managers' into WHERE dept_name = 'MANAGERS'."""
    planner = QueryIntentPlanner(schema=employees_schema)
    context = {
        "question": "Show all department managers",
        "selected_tables": ["departments", "dept_manager"],
        "join_plan": {
            "join_edges": [
                {"from": "departments", "from_column": "dept_no", "to": "dept_manager", "to_column": "dept_no"}
            ]
        },
        "predicate_manifest": {"filters": []},
    }
    intent = planner.build(context)
    # Ensure no filter sets dept_name = 'MANAGERS'
    for f in intent.filters:
        assert not (f.column == "dept_name" and str(f.value).upper() == "MANAGERS"), (
            "Found bogus hallucinated filter WHERE dept_name = 'MANAGERS'!"
        )


def test_join_provenance_recording(employees_schema):
    """Test that all join path steps record valid schema/FK provenance."""
    rel_builder = RelationshipBuilder(employees_schema)
    join_finder = JoinPathFinder(rel_builder)

    plan = join_finder.find_multi_table_join(["departments", "dept_manager"])
    assert len(plan["join_edges"]) == 1
    edge = plan["join_edges"][0]
    assert edge["from"] == "departments"
    assert edge["to"] == "dept_manager"
    assert edge["relationship_type"] in ("foreign_key", "mysql_foreign_key")
    assert "provenance" in edge


def test_sql_validator_rejects_nonexistent_columns(employees_schema):
    """Test that SQLValidator rejects queries referencing hallucinated columns."""
    validator = SQLValidator()
    tables_by_name = {t["name"]: t for t in employees_schema["tables"]}
    context = {
        "selected_tables": ["employees"],
        "tables_by_name": tables_by_name,
    }

    # Valid query
    valid_sql = "SELECT emp_no, first_name FROM employees WHERE gender = 'F'"
    res = validator.validate(valid_sql, context)
    assert "employees" in res

    # Invalid query referencing non-existent column
    invalid_sql = "SELECT emp_no, nonexistent_col FROM employees WHERE gender = 'F'"
    with pytest.raises(SQLValidationError, match="does not exist on"):
        validator.validate(invalid_sql, context)


def test_sql_validator_rejects_disallowed_tables(employees_schema):
    """Test that SQLValidator rejects tables outside the approved context."""
    validator = SQLValidator()
    tables_by_name = {t["name"]: t for t in employees_schema["tables"]}
    context = {
        "selected_tables": ["employees"],
        "tables_by_name": tables_by_name,
    }

    # Query attempting to access unselected `salaries` table
    unapproved_sql = "SELECT e.emp_no, s.salary FROM employees e JOIN salaries s ON e.emp_no = s.emp_no"
    with pytest.raises(SQLValidationError, match="outside approved context"):
        validator.validate(unapproved_sql, context)


def test_multi_table_join_resolution(employees_schema):
    """Test that multiple genuine target tables are both correctly included and joined."""
    rel_builder = RelationshipBuilder(employees_schema)
    join_finder = JoinPathFinder(rel_builder)
    grouping_data = {"table_to_domain": {t["name"]: "Operations" for t in employees_schema["tables"]}}
    semantics_data = {"tables": {}}
    ranking_data = {"rankings": [{"table": t["name"], "score": 1.0, "rank": i} for i, t in enumerate(employees_schema["tables"])]}

    generator = ContextGenerator(
        schema=employees_schema,
        grouping_data=grouping_data,
        semantics_data=semantics_data,
        ranking_data=ranking_data,
        join_finder=join_finder,
        top_n=3,
    )

    # "Show the salary history for employee 10001" requires both `employees` and `salaries`
    context = generator.build_context("Show the salary history for employee 10001")
    assert "employees" in context["selected_tables"]
    assert "salaries" in context["selected_tables"]
    assert len(context["join_plan"]["join_edges"]) >= 1


def test_pending_knowledge_isolation_from_prompt_and_context(employees_schema):
    """Test that pending knowledge draft items do not pollute prompt or approved context."""
    from services.sql_reasoning_engine import PromptBuilder

    context = {
        "question": "Show all departments",
        "selected_tables": ["departments"],
        "prompt": "## Database Task\nGenerate SQL\n\n## Database Schema\n### departments\n\n## User Request\nShow all departments\n\n## SQL Query:\n",
        "_defined_knowledge": type("MockRepo", (), {"all": lambda self: {"departments": {"definition": "Approved context for departments"}}})(),
        "_pending_knowledge": type("MockRepo", (), {"all": lambda self: {"departments": {"draft_context": "UNAPPROVED PENDING DRAFT"}}})(),
    }

    builder = PromptBuilder()
    rendered = builder.build(context)
    assert "Approved context for departments" in rendered
    assert "UNAPPROVED PENDING DRAFT" not in rendered


def test_abbreviation_grounding_with_defined_knowledge(employees_schema):
    """Test that approved abbreviations ground user terminology to the correct table."""
    from services.sql_reasoning_engine import PromptBuilder

    defined_knowledge = {
        "departments": {
            "definition": "Stores company department details.",
            "abbreviations": {"DEPT": {"meaning": "Department"}},
        }
    }

    context = {
        "question": "List all depts and their identifiers",
        "selected_tables": ["departments"],
        "prompt": "## Database Task\nGenerate SQL\n\n## Database Schema\n### departments\n\n## User Request\nList all depts\n\n## SQL Query:\n",
        "_defined_knowledge": type("MockRepo", (), {"all": lambda self: defined_knowledge})(),
    }

    builder = PromptBuilder()
    rendered = builder.build(context)
    assert "DEPT = Department" in rendered
    assert "Stores company department details." in rendered
