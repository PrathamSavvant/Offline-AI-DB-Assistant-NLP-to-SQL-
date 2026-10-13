import json
from pathlib import Path
import re

from acceptance.live_fk_acceptance import EMPLOYEE_COUNT, ORACLE_PATH, SCHEMA_PATH, TEST_DATABASE


REQUIRED_TABLES = {
    "organizations", "commands", "departments", "locations", "employees", "managers",
    "roles", "employee_roles", "projects", "employee_projects", "assignments",
    "historical_assignments", "teams", "team_members", "offices", "office_sections",
    "employee_office_sections", "salaries", "certifications", "audit_events",
}
REQUIRED_CATEGORIES = {
    "single_table", "direct_fk", "multi_hop_fk", "many_to_many", "self_reference",
    "approved_abbreviation", "table_context", "current_historical", "ambiguous_fk",
    "composite_fk", "no_fk_path", "non_database", "pending_isolation", "similar_columns",
}


def _schema_sql():
    return SCHEMA_PATH.read_text(encoding="utf-8")


def test_acceptance_schema_is_isolated_fk_heavy_and_deterministic():
    sql = _schema_sql()
    tables = set(re.findall(r"CREATE TABLE\s+([a-z_]+)", sql, re.IGNORECASE))
    assert tables == REQUIRED_TABLES
    assert 15 <= len(tables) <= 25
    assert TEST_DATABASE == "ai_assistant_fk_test"
    assert EMPLOYEE_COUNT == 120
    assert "DROP DATABASE" not in sql.upper()


def test_acceptance_schema_contains_required_adversarial_relationships():
    sql = " ".join(_schema_sql().split()).lower()
    assert "foreign key (department_id) references departments(department_id)" in sql
    assert "foreign key (manager_id) references employees(employee_id)" in sql
    assert "foreign key (office_id, section_code) references office_sections(office_id, section_code)" in sql
    assert "fk_employees_home_location" in sql and "fk_employees_work_location" in sql
    assert "fk_employee_projects_employee" in sql and "fk_employee_projects_project" in sql
    assert "fk_employee_roles_employee" in sql and "fk_employee_roles_role" in sql
    audit_block = sql.split("create table audit_events", 1)[1]
    assert "foreign key" not in audit_block


def test_oracle_has_complete_unique_structural_expectations():
    payload = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert payload["database"] == TEST_DATABASE
    assert len(cases) >= 40
    assert len({case["id"] for case in cases}) == len(cases)
    assert REQUIRED_CATEGORIES <= {case["category"] for case in cases}
    required = {
        "id", "category", "question", "expected_intent", "endpoint_tables",
        "bridge_tables", "constraints", "predicates", "llama_called", "sql_executed", "outcome",
    }
    assert all(required <= set(case) for case in cases)


def test_oracle_includes_safety_and_composite_cases():
    cases = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))["cases"]
    assert any(case["category"] == "composite_fk" and "fk_employee_office_sections_section" in case["constraints"] for case in cases)
    assert any(case["category"] == "ambiguous_fk" and not case["llama_called"] for case in cases)
    assert any(case["category"] == "no_fk_path" and not case["sql_executed"] for case in cases)
    assert any(case["category"] == "pending_isolation" and not case["llama_called"] for case in cases)
    assert any(case["question"] == "hello" and case["expected_intent"] == "CONVERSATION" for case in cases)
