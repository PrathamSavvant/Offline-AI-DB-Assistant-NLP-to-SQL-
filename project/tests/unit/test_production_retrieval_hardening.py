from core.context_generator import ContextGenerator
from core.inverted_index import InvertedTableIndex
from core.join_path_finder import JoinPathFinder
from core.predicate_planner import PredicatePlanner
from core.rag_layer import RAGLayer
from core.relationship_builder import RelationshipBuilder
from core.value_resolver import ValueResolver


class _ApprovedKnowledge:
    def __init__(self, entries):
        self.entries = entries

    def predicate_candidates(self, *args, **kwargs):
        return []

    def table(self, name):
        return self.entries.get(name, {})


def _table(name, columns, foreign_keys=None):
    return {
        "name": name,
        "columns": [
            {"name": column, "datatype": "int" if column.endswith("_id") else "varchar",
             "is_primary_key": column in {"employee_id", "project_id"}}
            for column in columns
        ],
        "foreign_keys": foreign_keys or [],
    }


def _rag(schema, defined, inverted_index=None):
    return RAGLayer(
        schema=schema,
        grouping_data={"table_to_domain": {}},
        semantics_data={"tables": {}},
        ranking_data={"rankings": []},
        relationship_graph={table["name"]: [] for table in schema["tables"]},
        defined_knowledge=defined,
        inverted_index=inverted_index,
    )


def test_exact_entity_match_drops_definition_that_only_mentions_that_entity():
    schema = {"tables": [
        _table("departments", ["department_id", "name"]),
        _table("commands", ["command_id", "name"]),
    ]}
    defined = {
        "departments": {"definition": "Contains organizational departments and identifying codes."},
        "commands": {"definition": "Contains operational commands that departments belong to."},
    }
    selected, _ = _rag(schema, defined).retrieve_relevant_tables("show all departments", top_n=4)
    assert [item["table"] for item in selected] == ["departments"]


def test_opaque_table_is_found_from_distinctive_approved_definition_in_300_tables():
    tables = [_table(f"erp_{index:03d}_x", ["row_id", "status"]) for index in range(299)]
    target = _table("ge_mg_19kva_cl_a", ["row_id", "recovery_status", "amount"])
    schema = {"tables": [*tables, target]}
    defined = {
        target["name"]: {
            "definition": "Contains finance payment recoveries and debt collection transactions."
        }
    }
    index = InvertedTableIndex()
    index.build(schema, defined_knowledge=defined)
    selected, trace = _rag(schema, defined, index).retrieve_relevant_tables(
        "show finance payment recoveries", top_n=4
    )
    assert [item["table"] for item in selected] == [target["name"]]
    assert len(trace.candidate_union["scored_candidates"]) <= 30


def test_definition_relationship_mentions_do_not_become_extra_endpoints():
    schema = {"tables": [
        _table("employees", ["employee_id", "department_id", "name"]),
        _table("departments", ["department_id", "name"]),
        _table("assignments", ["assignment_id", "employee_id", "command_id"]),
    ]}
    defined = {
        "employees": {"definition": "Contains employee identity and department membership."},
        "departments": {"definition": "Contains organizational departments."},
        "assignments": {"definition": "Contains employees current command assignments."},
    }
    selected, _ = _rag(schema, defined).retrieve_relevant_tables(
        "show employees with their departments", top_n=4
    )
    assert {item["table"] for item in selected} == {"employees", "departments"}


def test_pure_association_table_is_a_bridge_not_a_requested_endpoint():
    schema = {"database": "test", "tables": [
        _table("employees", ["employee_id", "name"]),
        _table("projects", ["project_id", "name"]),
        {
            "name": "employee_projects",
            "columns": [
                {"name": "employee_id", "datatype": "int", "is_primary_key": True},
                {"name": "project_id", "datatype": "int", "is_primary_key": True},
                {"name": "allocation_percent", "datatype": "int"},
            ],
            "foreign_keys": [
                {"table": "employee_projects", "column": "employee_id", "references_table": "employees", "references_column": "employee_id", "constraint_name": "fk_ep_employee", "verified": True, "provenance": "mysql_information_schema"},
                {"table": "employee_projects", "column": "project_id", "references_table": "projects", "references_column": "project_id", "constraint_name": "fk_ep_project", "verified": True, "provenance": "mysql_information_schema"},
            ],
        },
    ]}
    finder = JoinPathFinder(RelationshipBuilder(schema))
    generator = ContextGenerator(
        schema, {"table_to_domain": {}}, {"tables": {}}, {"rankings": []}, finder,
        top_n=4,
    )
    context = generator.build_context("show employees and their projects")
    assert set(context["join_plan"]["requested_tables"]) == {"employees", "projects"}
    assert context["bridge_tables"] == ["employee_projects"]


def test_grammar_words_never_become_lookup_values():
    schema = {"tables": [
        _table("employees", ["employee_id", "department_id", "name"]),
        _table("departments", ["department_id", "name"]),
    ]}
    resolver = ValueResolver(schema)
    for question in (
        "show employees with their department names",
        "which department has the most employees",
    ):
        context = {"question": question, "selected_tables": ["employees", "departments"]}
        manifest = PredicatePlanner(schema, value_resolver=resolver).plan(context)
        assert manifest["filters"] == []


def test_approved_abbreviation_with_column_becomes_exact_predicate():
    schema = {"tables": [_table("commands", ["command_id", "name"])]}
    knowledge = _ApprovedKnowledge({
        "commands": {"abbreviations": {
            "CC": {"meaning": "Central Command", "column": "name"}
        }}
    })
    resolver = ValueResolver(schema, defined_knowledge=knowledge)
    values = resolver.resolve({"question": "employees in CC", "selected_tables": ["commands"]})
    assert [(item.table, item.column, item.value, item.source) for item in values] == [
        ("commands", "name", "Central Command", "Knowledge Layer")
    ]


def test_missing_nullable_self_relationship_becomes_is_null_predicate():
    employees = _table("employees", ["employee_id", "manager_id", "name"], [{
        "table": "employees", "column": "manager_id", "references_table": "employees",
        "references_column": "employee_id", "constraint_name": "fk_manager",
        "verified": True, "provenance": "mysql_information_schema",
    }])
    next(column for column in employees["columns"] if column["name"] == "manager_id")["nullable"] = True
    schema = {"tables": [employees]}
    manifest = PredicatePlanner(schema).plan({
        "question": "employees without a manager", "selected_tables": ["employees"]
    })
    assert manifest["filters"] == [{
        "table": "employees", "column": "manager_id", "operator": "IS", "value": None,
        "confidence": 1.0, "reason": "explicit missing verified relationship",
        "source": "Predicate Planner", "semantic_type": "UNKNOWN",
    }]


def test_nearest_requested_fk_endpoint_is_connected_before_farther_ambiguity():
    schema = {"tables": [
        _table("employees", ["employee_id", "department_id"], [{
            "table": "employees", "column": "department_id", "references_table": "departments",
            "references_column": "department_id", "constraint_name": "fk_emp_department",
            "verified": True, "provenance": "mysql_information_schema",
        }]),
        _table("departments", ["department_id", "command_id"], [{
            "table": "departments", "column": "command_id", "references_table": "commands",
            "references_column": "command_id", "constraint_name": "fk_department_command",
            "verified": True, "provenance": "mysql_information_schema",
        }]),
        _table("commands", ["command_id", "name"]),
        _table("assignments", ["employee_id", "command_id"], [
            {"table": "assignments", "column": "employee_id", "references_table": "employees", "references_column": "employee_id", "constraint_name": "fk_assignment_employee", "verified": True, "provenance": "mysql_information_schema"},
            {"table": "assignments", "column": "command_id", "references_table": "commands", "references_column": "command_id", "constraint_name": "fk_assignment_command", "verified": True, "provenance": "mysql_information_schema"},
        ]),
    ]}
    plan = JoinPathFinder(RelationshipBuilder(schema)).find_multi_table_join(
        ["employees", "departments", "commands"], allow_semantic_fallback=False,
        fail_on_ambiguous_paths=True,
    )
    assert plan["ambiguous_paths"] == []
    assert plan["unreachable_tables"] == []
    assert "assignments" not in plan["included_tables"]


def test_approved_knowledge_can_choose_between_equal_verified_fk_bridges():
    schema = {"tables": [
        _table("employees", ["employee_id"]),
        _table("commands", ["command_id"]),
        _table("assignments", ["employee_id", "command_id"], [
            {"table": "assignments", "column": "employee_id", "references_table": "employees", "references_column": "employee_id", "constraint_name": "fk_assignment_employee", "verified": True, "provenance": "mysql_information_schema"},
            {"table": "assignments", "column": "command_id", "references_table": "commands", "references_column": "command_id", "constraint_name": "fk_assignment_command", "verified": True, "provenance": "mysql_information_schema"},
        ]),
        _table("department_memberships", ["employee_id", "command_id"], [
            {"table": "department_memberships", "column": "employee_id", "references_table": "employees", "references_column": "employee_id", "constraint_name": "fk_membership_employee", "verified": True, "provenance": "mysql_information_schema"},
            {"table": "department_memberships", "column": "command_id", "references_table": "commands", "references_column": "command_id", "constraint_name": "fk_membership_command", "verified": True, "provenance": "mysql_information_schema"},
        ]),
    ]}
    plan = JoinPathFinder(RelationshipBuilder(schema)).find_multi_table_join(
        ["employees", "commands"], allow_semantic_fallback=False,
        fail_on_ambiguous_paths=True, preferred_bridge_tables=["assignments"],
    )
    assert plan["ambiguous_paths"] == []
    assert "assignments" in plan["included_tables"]
    assert "department_memberships" not in plan["included_tables"]


def test_approved_temporal_definitions_disambiguate_sibling_tables():
    schema = {"tables": [
        _table("assignments", ["assignment_id", "employee_id"]),
        _table("historical_assignments", ["assignment_id", "employee_id"]),
    ]}
    defined = {
        "assignments": {"definition": "Contains current employee assignments."},
        "historical_assignments": {"definition": "Contains past employee assignments that are no longer current."},
    }
    selected, _ = _rag(schema, defined).retrieve_relevant_tables(
        "show previous employee assignments", top_n=4
    )
    assert [item["table"] for item in selected] == ["historical_assignments"]
