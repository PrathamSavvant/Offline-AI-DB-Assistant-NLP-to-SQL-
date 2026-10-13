from core.business_intent_resolver import BusinessIntentResolver
from core.join_path_finder import JoinPathFinder
from core.relationship_builder import RelationshipBuilder


def test_definition_context_cannot_add_an_unrequested_table():
    schema = {"tables": [
        {"name": "employees", "columns": [{"name": "emp_no"}], "foreign_keys": []},
        {"name": "departments", "columns": [{"name": "dept_no"}], "foreign_keys": []},
    ]}
    knowledge = {
        "employees": {"aliases": ["people"], "definition": "people employed by the company"},
        "departments": {"definition": "organizational units in the company"},
    }
    intent = BusinessIntentResolver(schema, defined_knowledge=knowledge).resolve(
        "How many people are there in the company?"
    )
    assert intent.primary_table == "employees"
    assert intent.required_tables == ["employees"]
    assert intent.result_type == "COUNT"
    assert {item["table"] for item in intent.ignored_tables} == {"departments"}


def test_generated_business_tag_cannot_promote_a_junction_table_for_simple_count():
    schema = {"tables": [
        {"name": "employees", "columns": [{"name": "EmployeeID"}], "foreign_keys": []},
        {"name": "employeeprojects", "columns": [{"name": "EmployeeID"}], "foreign_keys": []},
    ]}
    semantics = {"tables": {
        "employees": {"business_tags": ["employee"]},
        "employeeprojects": {"business_tags": ["employee", "project"]},
    }}
    intent = BusinessIntentResolver(schema, semantics=semantics).resolve(
        "how many employees in the company", fallback_tables=["employees"]
    )
    assert intent.primary_table == "employees"
    assert intent.required_tables == ["employees"]


def test_opaque_tables_are_resolved_from_approved_entity_predicate_and_grouping_language():
    schema = {"tables": [
        {"name": "x_a17", "columns": [{"name": "state_cd"}], "foreign_keys": []},
        {"name": "x_b04", "columns": [{"name": "label_tx"}], "foreign_keys": []},
    ]}
    knowledge = {
        "x_a17": {
            "business_entity": "repair equipment",
            "aliases": ["equipment"],
            "predicate_mappings": [{
                "phrase": "under repair", "column": "state_cd", "value": "REPAIR",
                "approval_status": "USER_VERIFIED",
            }],
        },
        "x_b04": {"business_entity": "workshop group", "aliases": ["group"]},
    }
    intent = BusinessIntentResolver(schema, defined_knowledge=knowledge).resolve(
        "equipment under repair in OE group"
    )
    assert intent.primary_table == "x_a17"
    assert intent.required_tables == ["x_a17", "x_b04"]
    assert intent.confidence > 0.7


def test_same_approved_alias_for_two_tables_requires_clarification():
    schema = {"tables": [
        {"name": "a", "columns": [], "foreign_keys": []},
        {"name": "b", "columns": [], "foreign_keys": []},
    ]}
    knowledge = {"a": {"aliases": ["asset"]}, "b": {"aliases": ["asset"]}}
    intent = BusinessIntentResolver(schema, defined_knowledge=knowledge).resolve("show asset")
    assert intent.ambiguities
    assert intent.ambiguities[0]["tables"] == ["a", "b"]


def test_shorter_approved_business_relationship_beats_long_fk_chain_when_planning_join():
    schema = {"database": "workshop", "tables": [
        {"name": "equipment", "columns": [{"name": "id"}, {"name": "group_code"}],
         "foreign_keys": [{"table": "equipment", "column": "id", "references_table": "bridge",
                           "references_column": "equipment_id", "constraint_name": "fk_equipment_bridge"}]},
        {"name": "bridge", "columns": [{"name": "equipment_id"}, {"name": "group_id"}],
         "foreign_keys": [{"table": "bridge", "column": "group_id", "references_table": "groups",
                           "references_column": "id", "constraint_name": "fk_bridge_group"}]},
        {"name": "groups", "columns": [{"name": "id"}], "foreign_keys": []},
    ]}
    approved = [{
        "relationship_id": "approved-equipment-group", "from_table": "equipment",
        "from_column": "group_code", "to_table": "groups", "to_column": "id",
        "status": "approved", "description": "equipment assigned to workshop group",
    }]
    finder = JoinPathFinder(RelationshipBuilder(schema, approved))
    plan = finder.find_multi_table_join(
        ["equipment", "groups"], question="equipment in workshop group",
        allow_semantic_fallback=True, fail_on_ambiguous_paths=True,
    )
    assert len(plan["join_edges"]) == 1
    assert plan["join_edges"][0]["relationship_type"] == "semantic"
    assert plan["all_joins_approved"] is True


def test_pending_business_relationship_is_never_available_to_join_planner():
    schema = {"tables": [
        {"name": "a", "columns": [{"name": "id"}], "foreign_keys": []},
        {"name": "b", "columns": [{"name": "id"}], "foreign_keys": []},
    ]}
    pending = [{"relationship_id": "pending", "from_table": "a", "from_column": "id",
                "to_table": "b", "to_column": "id", "status": "pending"}]
    plan = JoinPathFinder(RelationshipBuilder(schema, pending)).find_multi_table_join(
        ["a", "b"], question="show a with b", allow_semantic_fallback=True,
    )
    assert plan["join_edges"] == []
    assert plan["unreachable_tables"] == ["b"]
