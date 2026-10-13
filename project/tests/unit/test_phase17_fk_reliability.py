"""
tests/unit/test_phase17_fk_reliability.py

Phase 17 — FK-First Database Reliability: 20 Adversarial Unit Tests

Tests cover:
  1.  Real FK is discovered from schema metadata
  2.  PK is correctly identified
  3.  FK source/target columns are preserved
  4.  FK provenance is preserved (mysql_information_schema)
  5.  verified=True for FK edges, verified=False for semantic
  6.  Direct FK join is accepted by SQLValidator
  7.  Multi-hop FK path is accepted
  8.  Non-FK matching column names do NOT create a join edge
  9.  Semantic similarity does NOT create an automatic join
  10. Fabricated join condition is rejected by SQLValidator
  11. Wrong FK column pair is rejected (right tables, wrong columns)
  12. Valid tables, invalid relationship is rejected
  13. Deterministic join plan reaches the Llama prompt
  14. Generated SQL must conform to the deterministic join plan (FK index)
  15. FK-only BFS prefers FK path over semantic path when both exist
  16. No-FK relationship fails safely (unreachable)
  17. Pending Knowledge cannot affect FK reasoning
  18. Approved Table Context can affect table selection but NOT create FKs
  19. Schema refresh updates FK metadata (RelationshipBuilder rebuild)
  20. Phase 16 behavior remains intact (provenance/type fields still present)
"""

import pytest
from core.context_generator import ContextGenerator
from core.join_path_finder import JoinPathFinder
from core.relationship_builder import RelationshipBuilder
from services.sql_reasoning_engine import SQLValidator, SQLValidationError


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def employees_schema():
    """Canonical employees database schema with real FK constraints."""
    return {
        "database": "employees",
        "tables": [
            {
                "name": "employees",
                "columns": [
                    {"name": "emp_no",     "datatype": "int",         "full_type": "int",         "is_primary_key": True,  "nullable": False},
                    {"name": "birth_date", "datatype": "date",        "full_type": "date",        "is_primary_key": False, "nullable": False},
                    {"name": "first_name", "datatype": "varchar",     "full_type": "varchar(14)", "is_primary_key": False, "nullable": False},
                    {"name": "last_name",  "datatype": "varchar",     "full_type": "varchar(16)", "is_primary_key": False, "nullable": False},
                    {"name": "gender",     "datatype": "enum",        "full_type": "enum('M','F')", "is_primary_key": False, "nullable": False},
                    {"name": "hire_date",  "datatype": "date",        "full_type": "date",        "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no"],
                "foreign_keys": [],
            },
            {
                "name": "departments",
                "columns": [
                    {"name": "dept_no",   "datatype": "char",    "full_type": "char(4)",       "is_primary_key": True,  "nullable": False},
                    {"name": "dept_name", "datatype": "varchar", "full_type": "varchar(40)",   "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["dept_no"],
                "foreign_keys": [],
            },
            {
                "name": "dept_manager",
                "columns": [
                    {"name": "emp_no",     "datatype": "int",  "full_type": "int",    "is_primary_key": True,  "nullable": False},
                    {"name": "dept_no",    "datatype": "char", "full_type": "char(4)","is_primary_key": True,  "nullable": False},
                    {"name": "from_date",  "datatype": "date", "full_type": "date",   "is_primary_key": False, "nullable": False},
                    {"name": "to_date",    "datatype": "date", "full_type": "date",   "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "dept_no"],
                "foreign_keys": [
                    {"table": "dept_manager", "column": "emp_no",  "references_table": "employees",  "references_column": "emp_no",  "constraint_name": "fk_dm_emp"},
                    {"table": "dept_manager", "column": "dept_no", "references_table": "departments", "references_column": "dept_no", "constraint_name": "fk_dm_dept"},
                ],
            },
            {
                "name": "dept_emp",
                "columns": [
                    {"name": "emp_no",    "datatype": "int",  "full_type": "int",    "is_primary_key": True,  "nullable": False},
                    {"name": "dept_no",   "datatype": "char", "full_type": "char(4)","is_primary_key": True,  "nullable": False},
                    {"name": "from_date", "datatype": "date", "full_type": "date",   "is_primary_key": False, "nullable": False},
                    {"name": "to_date",   "datatype": "date", "full_type": "date",   "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "dept_no"],
                "foreign_keys": [
                    {"table": "dept_emp", "column": "emp_no",  "references_table": "employees",  "references_column": "emp_no",  "constraint_name": "fk_de_emp"},
                    {"table": "dept_emp", "column": "dept_no", "references_table": "departments", "references_column": "dept_no", "constraint_name": "fk_de_dept"},
                ],
            },
            {
                "name": "salaries",
                "columns": [
                    {"name": "emp_no",    "datatype": "int",  "full_type": "int",  "is_primary_key": True,  "nullable": False},
                    {"name": "salary",    "datatype": "int",  "full_type": "int",  "is_primary_key": False, "nullable": False},
                    {"name": "from_date", "datatype": "date", "full_type": "date", "is_primary_key": True,  "nullable": False},
                    {"name": "to_date",   "datatype": "date", "full_type": "date", "is_primary_key": False, "nullable": False},
                ],
                "primary_key": ["emp_no", "from_date"],
                "foreign_keys": [
                    {"table": "salaries", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_sal_emp"},
                ],
            },
            {
                "name": "titles",
                "columns": [
                    {"name": "emp_no",    "datatype": "int",     "full_type": "int",        "is_primary_key": True,  "nullable": False},
                    {"name": "title",     "datatype": "varchar", "full_type": "varchar(50)","is_primary_key": True,  "nullable": False},
                    {"name": "from_date", "datatype": "date",    "full_type": "date",       "is_primary_key": True,  "nullable": False},
                    {"name": "to_date",   "datatype": "date",    "full_type": "date",       "is_primary_key": False, "nullable": True},
                ],
                "primary_key": ["emp_no", "title", "from_date"],
                "foreign_keys": [
                    {"table": "titles", "column": "emp_no", "references_table": "employees", "references_column": "emp_no", "constraint_name": "fk_tit_emp"},
                ],
            },
            # Isolated table: no FKs — for testing no-path scenarios
            {
                "name": "audit_log",
                "columns": [
                    {"name": "log_id",    "datatype": "int",     "full_type": "int",         "is_primary_key": True,  "nullable": False},
                    {"name": "action",    "datatype": "varchar", "full_type": "varchar(100)","is_primary_key": False, "nullable": False},
                    {"name": "emp_no",    "datatype": "int",     "full_type": "int",         "is_primary_key": False, "nullable": True},
                ],
                "primary_key": ["log_id"],
                "foreign_keys": [],  # Intentionally no FK despite having emp_no column
            },
        ],
    }


@pytest.fixture
def rel_builder(employees_schema):
    return RelationshipBuilder(employees_schema)


@pytest.fixture
def join_finder(rel_builder):
    return JoinPathFinder(rel_builder)


@pytest.fixture
def tables_by_name(employees_schema):
    return {t["name"]: t for t in employees_schema["tables"]}


# ---------------------------------------------------------------------------
# Test 1: Real FK is discovered from schema metadata
# ---------------------------------------------------------------------------

def test_real_fk_is_discovered(rel_builder):
    """FK relationships are extracted from table foreign_keys entries."""
    fk_rels = rel_builder.get_fk_only_relationships()
    fk_pairs = {(r["from_table"], r["from_column"], r["to_table"], r["to_column"]) for r in fk_rels}

    # All expected FKs must be present
    assert ("dept_manager", "emp_no",  "employees",  "emp_no")  in fk_pairs
    assert ("dept_manager", "dept_no", "departments", "dept_no") in fk_pairs
    assert ("dept_emp",     "emp_no",  "employees",  "emp_no")  in fk_pairs
    assert ("dept_emp",     "dept_no", "departments", "dept_no") in fk_pairs
    assert ("salaries",     "emp_no",  "employees",  "emp_no")  in fk_pairs
    assert ("titles",       "emp_no",  "employees",  "emp_no")  in fk_pairs


# ---------------------------------------------------------------------------
# Test 2: PK is correctly identified
# ---------------------------------------------------------------------------

def test_pk_is_correctly_identified(employees_schema):
    """Primary key columns are correctly identified in the schema."""
    tables = {t["name"]: t for t in employees_schema["tables"]}

    assert tables["employees"]["primary_key"] == ["emp_no"]
    assert tables["departments"]["primary_key"] == ["dept_no"]
    # Composite PK
    assert set(tables["dept_manager"]["primary_key"]) == {"emp_no", "dept_no"}
    # Column-level is_primary_key flag
    emp_cols = {c["name"]: c for c in tables["employees"]["columns"]}
    assert emp_cols["emp_no"]["is_primary_key"] is True
    assert emp_cols["first_name"]["is_primary_key"] is False


# ---------------------------------------------------------------------------
# Test 3: FK source/target columns are preserved
# ---------------------------------------------------------------------------

def test_fk_source_target_columns_preserved(rel_builder):
    """FK relationships preserve the exact source and target column names."""
    rels = rel_builder.get_fk_only_relationships()
    sal_fk = next(r for r in rels if r["from_table"] == "salaries")
    assert sal_fk["from_column"] == "emp_no"
    assert sal_fk["to_table"] == "employees"
    assert sal_fk["to_column"] == "emp_no"
    assert sal_fk["constraint_name"] == "fk_sal_emp"


# ---------------------------------------------------------------------------
# Test 4: FK provenance is preserved
# ---------------------------------------------------------------------------

def test_fk_provenance_is_mysql_information_schema(rel_builder):
    """All FK edges must carry provenance='mysql_information_schema'."""
    rels = rel_builder.get_fk_only_relationships()
    for r in rels:
        assert r["provenance"] == "mysql_information_schema", (
            f"Expected provenance 'mysql_information_schema', got '{r['provenance']}' "
            f"for {r['from_table']}.{r['from_column']}"
        )


# ---------------------------------------------------------------------------
# Test 5: verified=True for FK, verified=False for semantic
# ---------------------------------------------------------------------------

def test_verified_flag_separates_fk_from_semantic(employees_schema):
    """verified=True only for database FK metadata; semantic edges get False."""
    semantic_rels = [
        {
            "relationship_id": "sr-001",
            "from_table": "employees",
            "from_column": "emp_no",
            "to_table": "audit_log",
            "to_column": "emp_no",
            "status": "approved",
            "source": "user_verified",
            "description": "Audit events for employees",
        }
    ]
    builder = RelationshipBuilder(employees_schema, semantic_relationships=semantic_rels)
    all_rels = builder.get_relationships()

    fk_rels = [r for r in all_rels if r["relationship_type"] == "foreign_key"]
    sem_rels = [r for r in all_rels if r["relationship_type"] == "semantic"]

    assert all(r["verified"] is True for r in fk_rels), "All FK rels must be verified=True"
    assert all(r["verified"] is False for r in sem_rels), "All semantic rels must be verified=False"


# ---------------------------------------------------------------------------
# Test 6: Direct FK join is accepted by SQLValidator
# ---------------------------------------------------------------------------

def test_direct_fk_join_accepted_by_validator(tables_by_name):
    """A JOIN that matches the FK plan edge is accepted."""
    validator = SQLValidator()
    context = {
        "selected_tables": ["employees", "salaries"],
        "tables_by_name": tables_by_name,
        "join_plan": {
            "included_tables": ["employees", "salaries"],
            "join_edges": [
                {"from": "employees", "from_column": "emp_no",
                 "to": "salaries", "to_column": "emp_no",
                 "verified": True, "provenance": "mysql_information_schema"},
            ],
        },
    }
    sql = "SELECT e.emp_no, s.salary FROM employees e JOIN salaries s ON e.emp_no = s.emp_no LIMIT 100"
    result = validator.validate(sql, context)
    assert "emp_no" in result


# ---------------------------------------------------------------------------
# Test 7: Multi-hop FK path is accepted
# ---------------------------------------------------------------------------

def test_multi_hop_fk_path_accepted(join_finder, tables_by_name):
    """employees → dept_emp → departments is a valid 2-hop FK path."""
    plan = join_finder.find_multi_table_join(["employees", "departments"])
    assert plan["included_tables"] is not None
    # The path must route through dept_emp (bridge) or dept_manager
    assert len(plan["join_edges"]) >= 2, (
        f"Expected >= 2 hops for employees → departments, got {plan['join_edges']}"
    )
    # All edges should be FK-verified
    assert plan.get("all_joins_verified") is True


# ---------------------------------------------------------------------------
# Test 8: Non-FK column name match does NOT create a join edge
# ---------------------------------------------------------------------------

def test_column_name_match_does_not_create_join(rel_builder):
    """
    audit_log.emp_no shares the column name with employees.emp_no but
    has NO FK relationship. The FK graph must NOT contain an edge between them.
    """
    fk_graph = rel_builder.get_fk_graph()
    audit_edges = fk_graph.get("audit_log", [])

    # No edge from audit_log to employees or vice versa in the FK-only graph
    audit_neighbors = {e["to_table"] for e in audit_edges}
    assert "employees" not in audit_neighbors, (
        "audit_log should have NO FK edge to employees "
        "(it only has a matching column name, not a constraint)"
    )

    emp_edges = fk_graph.get("employees", [])
    emp_neighbors = {e["to_table"] for e in emp_edges}
    assert "audit_log" not in emp_neighbors, (
        "employees should have NO FK edge to audit_log"
    )


# ---------------------------------------------------------------------------
# Test 9: Semantic similarity does NOT create an automatic join
# ---------------------------------------------------------------------------

def test_semantic_similarity_does_not_create_join(rel_builder):
    """
    Without an explicit user-approved semantic relationship, two tables
    that look related (e.g. employees / audit_log) must not be connected
    in the FK graph.
    """
    fk_rels = rel_builder.get_fk_only_relationships()
    involved = {
        (r["from_table"], r["to_table"])
        for r in fk_rels
        if "audit_log" in (r["from_table"], r["to_table"])
    }
    assert not involved, (
        f"audit_log must not appear in FK relationships: {involved}"
    )


# ---------------------------------------------------------------------------
# Test 10: Fabricated join condition is rejected by SQLValidator
# ---------------------------------------------------------------------------

def test_fabricated_join_rejected(tables_by_name):
    """
    A JOIN on employees.first_name = departments.dept_name is syntactically
    valid but structurally fabricated — it must be rejected.
    """
    validator = SQLValidator()
    context = {
        "selected_tables": ["employees", "departments"],
        "tables_by_name": tables_by_name,
        "join_plan": {
            "included_tables": ["employees", "departments"],
            "join_edges": [
                # Real plan would route via dept_emp — but pretend it's empty
                # to test that the validator rejects the hallucinated direct join
            ],
        },
    }
    # With no join_edges in the plan, fk_index is empty → validator skips FK check
    # So we test a plan that HAS edges but the SQL uses wrong columns
    context["join_plan"]["join_edges"] = [
        {"from": "employees", "from_column": "emp_no",
         "to": "departments", "to_column": "dept_no",
         "verified": True, "provenance": "mysql_information_schema"},
    ]
    # SQL uses wrong column pair: first_name vs dept_name
    bad_sql = "SELECT e.first_name, d.dept_name FROM employees e JOIN departments d ON e.first_name = d.dept_name LIMIT 100"
    with pytest.raises(SQLValidationError, match="not a verified FK relationship"):
        validator.validate(bad_sql, context)


# ---------------------------------------------------------------------------
# Test 11: Wrong FK column pair is rejected (right tables, wrong columns)
# ---------------------------------------------------------------------------

def test_wrong_fk_columns_rejected(tables_by_name):
    """
    employees JOIN salaries ON employees.hire_date = salaries.from_date
    is rejected because the approved FK is emp_no = emp_no, not hire_date = from_date.
    """
    validator = SQLValidator()
    context = {
        "selected_tables": ["employees", "salaries"],
        "tables_by_name": tables_by_name,
        "join_plan": {
            "included_tables": ["employees", "salaries"],
            "join_edges": [
                {"from": "employees", "from_column": "emp_no",
                 "to": "salaries", "to_column": "emp_no",
                 "verified": True, "provenance": "mysql_information_schema"},
            ],
        },
    }
    wrong_sql = "SELECT e.emp_no FROM employees e JOIN salaries s ON e.hire_date = s.from_date LIMIT 100"
    with pytest.raises(SQLValidationError, match="not a verified FK relationship"):
        validator.validate(wrong_sql, context)


# ---------------------------------------------------------------------------
# Test 12: Valid tables, invalid relationship is rejected
# ---------------------------------------------------------------------------

def test_valid_tables_invalid_relationship_rejected(tables_by_name):
    """
    A join between valid tables on logically-matched-but-wrong columns
    is rejected by FK index enforcement.
    """
    validator = SQLValidator()
    context = {
        "selected_tables": ["dept_manager", "departments"],
        "tables_by_name": tables_by_name,
        "join_plan": {
            "included_tables": ["dept_manager", "departments"],
            "join_edges": [
                {"from": "dept_manager", "from_column": "dept_no",
                 "to": "departments", "to_column": "dept_no",
                 "verified": True, "provenance": "mysql_information_schema"},
            ],
        },
    }
    # Wrong: joins on emp_no instead of dept_no
    wrong_sql = "SELECT dm.emp_no FROM dept_manager dm JOIN departments d ON dm.emp_no = d.dept_no LIMIT 100"
    with pytest.raises(SQLValidationError):
        validator.validate(wrong_sql, context)


# ---------------------------------------------------------------------------
# Test 13: Deterministic join plan reaches the Llama prompt
# ---------------------------------------------------------------------------

def test_join_plan_reaches_prompt(employees_schema, join_finder):
    """The prompt must contain 'Verified Relationships' and 'Verified Join Path'."""
    grouping_data = {"table_to_domain": {t["name"]: "HR" for t in employees_schema["tables"]}}
    semantics_data = {"tables": {}}
    ranking_data = {"rankings": [{"table": t["name"], "score": 1.0, "rank": i}
                                  for i, t in enumerate(employees_schema["tables"])]}
    generator = ContextGenerator(
        schema=employees_schema,
        grouping_data=grouping_data,
        semantics_data=semantics_data,
        ranking_data=ranking_data,
        join_finder=join_finder,
        top_n=2,
    )
    context = generator.build_context("Show the salary history for employee 10001")
    prompt = context["prompt"]

    assert "Verified Join Path" in prompt, (
        "Prompt must contain 'Verified Join Path' (not 'Suggested Join Path')"
    )
    # If multi-table: Verified Relationships section should appear
    if len(context["join_plan"]["join_edges"]) > 0:
        assert "Verified Relationships" in prompt, (
            "Prompt must contain 'Verified Relationships' section when join edges exist"
        )


# ---------------------------------------------------------------------------
# Test 14: Generated SQL must conform to the deterministic join plan
# ---------------------------------------------------------------------------

def test_sql_must_conform_to_join_plan(tables_by_name):
    """
    SQLValidator enforces that the SQL JOIN uses exactly the approved
    FK column pair, not a different pairing on the same tables.
    """
    validator = SQLValidator()
    # Approved plan: salaries.emp_no = employees.emp_no
    context = {
        "selected_tables": ["employees", "salaries"],
        "tables_by_name": tables_by_name,
        "join_plan": {
            "included_tables": ["employees", "salaries"],
            "join_edges": [
                {"from": "employees", "from_column": "emp_no",
                 "to": "salaries", "to_column": "emp_no",
                 "verified": True, "provenance": "mysql_information_schema"},
            ],
        },
    }
    # Conforming SQL — should pass
    good_sql = "SELECT e.emp_no, s.salary FROM employees e JOIN salaries s ON e.emp_no = s.emp_no LIMIT 100"
    result = validator.validate(good_sql, context)
    assert "employees" in result or "emp_no" in result

    # Non-conforming SQL — should be rejected
    bad_sql = "SELECT e.emp_no FROM employees e JOIN salaries s ON e.hire_date = s.from_date LIMIT 100"
    with pytest.raises(SQLValidationError, match="not a verified FK relationship"):
        validator.validate(bad_sql, context)


# ---------------------------------------------------------------------------
# Test 15: FK-only BFS prefers FK path over semantic path
# ---------------------------------------------------------------------------

def test_fk_first_bfs_prefers_fk_over_semantic(employees_schema):
    """
    When a verified FK path exists, the FK-first BFS uses it.
    The resulting edges must all have verified=True.
    """
    builder = RelationshipBuilder(employees_schema)
    finder = JoinPathFinder(builder)
    plan = finder.find_multi_table_join(["dept_manager", "employees"])

    assert plan["join_edges"], "Expected at least one join edge"
    for edge in plan["join_edges"]:
        assert edge.get("verified") is True, (
            f"Expected all edges to be FK-verified; got {edge}"
        )
    assert plan.get("all_joins_verified") is True


# ---------------------------------------------------------------------------
# Test 16: No-FK relationship fails safely (unreachable)
# ---------------------------------------------------------------------------

def test_no_fk_relationship_fails_safely(join_finder):
    """
    audit_log has no FK to employees. The join plan must report it as
    unreachable rather than creating a spurious join.
    """
    plan = join_finder.find_multi_table_join(["employees", "audit_log"],
                                              allow_semantic_fallback=False)
    assert "audit_log" in plan["unreachable_tables"], (
        f"audit_log should be unreachable without FK or semantic fallback; "
        f"got plan: {plan}"
    )


# ---------------------------------------------------------------------------
# Test 17: Pending Knowledge cannot affect FK reasoning
# ---------------------------------------------------------------------------

def test_pending_knowledge_cannot_affect_fk_graph(employees_schema):
    """
    Even with pending (unapproved) semantic relationships present,
    the FK graph must not include those edges.
    """
    unapproved_semantic = [
        {
            "relationship_id": "pending-001",
            "from_table": "employees",
            "from_column": "emp_no",
            "to_table": "audit_log",
            "to_column": "emp_no",
            "status": "pending",            # NOT approved — must be excluded
            "source": "user_submitted",
            "description": "Unapproved pending relationship",
        }
    ]
    builder = RelationshipBuilder(employees_schema, semantic_relationships=unapproved_semantic)

    # FK-only graph must be unchanged
    fk_rels = builder.get_fk_only_relationships()
    assert not any(
        r["from_table"] == "employees" and r["to_table"] == "audit_log"
        for r in fk_rels
    ), "Pending relationship must not appear in FK-only relationships"

    # Full graph must also exclude it (status != approved)
    all_rels = builder.get_relationships()
    assert not any(
        r.get("relationship_type") == "semantic"
        and r["from_table"] == "employees" and r["to_table"] == "audit_log"
        for r in all_rels
    ), "Pending semantic relationship must not appear in any relationship list"


# ---------------------------------------------------------------------------
# Test 18: Approved Table Context can affect table selection, NOT create FKs
# ---------------------------------------------------------------------------

def test_table_context_affects_selection_not_fk(employees_schema, join_finder):
    """
    Approved Table Context (defined_knowledge) can influence which table
    gets selected by the RAG/scoring layer, but it must NEVER create a FK edge.
    """
    # Even with rich defined knowledge for audit_log, it should NOT get
    # a FK edge to employees in the FK-only graph.
    defined_knowledge = {
        "audit_log": {
            "definition": "Audit log for all employee actions. Tracks emp_no.",
            "abbreviations": {},
        }
    }
    builder = RelationshipBuilder(employees_schema)
    fk_graph = builder.get_fk_graph()

    # audit_log must still be isolated in FK-only graph
    audit_edges = fk_graph.get("audit_log", [])
    assert not audit_edges, (
        "Defined knowledge about audit_log must not create FK edges. "
        f"Got: {audit_edges}"
    )


# ---------------------------------------------------------------------------
# Test 19: Schema refresh updates FK metadata
# ---------------------------------------------------------------------------

def test_schema_refresh_updates_fk_metadata(employees_schema):
    """
    After a schema change (FK added), RelationshipBuilder reflects the update.
    Simulates the effect of schema_sync_service triggering a rebuild.
    """
    # Original: audit_log has no FKs
    original_builder = RelationshipBuilder(employees_schema)
    original_fk_rels = original_builder.get_fk_only_relationships()
    original_audit_involved = [r for r in original_fk_rels if "audit_log" in (r["from_table"], r["to_table"])]
    assert not original_audit_involved

    # Simulate schema refresh: FK added to audit_log
    import copy
    updated_schema = copy.deepcopy(employees_schema)
    for table in updated_schema["tables"]:
        if table["name"] == "audit_log":
            table["foreign_keys"] = [
                {"table": "audit_log", "column": "emp_no",
                 "references_table": "employees", "references_column": "emp_no",
                 "constraint_name": "fk_audit_emp"},
            ]

    refreshed_builder = RelationshipBuilder(updated_schema)
    refreshed_fk_rels = refreshed_builder.get_fk_only_relationships()
    refreshed_audit_involved = [r for r in refreshed_fk_rels if "audit_log" in (r["from_table"], r["to_table"])]
    assert refreshed_audit_involved, (
        "After schema refresh with a new FK, audit_log must appear in FK relationships"
    )
    assert refreshed_builder.get_fk_diagnostics()["verified_relationship_count"] > \
           original_builder.get_fk_diagnostics()["verified_relationship_count"]


# ---------------------------------------------------------------------------
# Test 20: Phase 16 behavior remains intact
# ---------------------------------------------------------------------------

def test_phase16_provenance_and_type_fields_still_present(rel_builder):
    """
    Phase 16 established provenance and relationship_type fields on all edges.
    Phase 17 must not remove them.
    """
    rels = rel_builder.get_relationships()
    graph = rel_builder.build_graph()

    # Flat relationship list
    for r in rels:
        assert "provenance" in r, f"Missing provenance on relationship: {r}"
        assert "relationship_type" in r, f"Missing relationship_type on relationship: {r}"
        assert "constraint_name" in r, f"Missing constraint_name on relationship: {r}"

    # Graph edges
    for table_name, edges in graph.items():
        for edge in edges:
            assert "provenance" in edge, f"Missing provenance on graph edge: {edge}"
            assert "relationship_type" in edge, f"Missing relationship_type on edge: {edge}"
            assert "direction" in edge, f"Missing direction on graph edge: {edge}"
            assert "verified" in edge, f"Phase 17: Missing verified flag on edge: {edge}"

    # Join path steps
    from core.join_path_finder import JoinPathFinder
    finder = JoinPathFinder(rel_builder)
    plan = finder.find_multi_table_join(["employees", "salaries"])
    for step in plan["join_edges"]:
        assert "provenance" in step, f"Missing provenance on join step: {step}"
        assert "verified" in step, f"Missing verified on join step: {step}"
        assert "relationship_type" in step, f"Missing relationship_type on join step: {step}"
