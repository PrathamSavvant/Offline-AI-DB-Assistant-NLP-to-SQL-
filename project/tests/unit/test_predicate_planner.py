"""tests/unit/test_predicate_planner.py

Unit tests for PredicatePlanner and PredicateManifest generation.
"""

from core.predicate_planner import PredicatePlanner, PredicateManifest, PlannedFilter
from core.value_resolver import ValueResolver

ERP_SCHEMA = {
    "tables": [
        {
            "name": "groups",
            "columns": [
                {"name": "id"},
                {"name": "name"},
                {"name": "status", "full_type": "enum('Active','Inactive')"},
            ],
        },
        {
            "name": "work_equipment",
            "columns": [
                {"name": "id"},
                {"name": "group_id"},
                {"name": "eqpt"},
                {"name": "sus", "full_type": "varchar(50)"},
                {"name": "repair_class"},
                {"name": "status", "full_type": "enum('Active','Inactive')"},
            ],
        },
        {
            "name": "work_orders",
            "columns": [
                {"name": "id"},
                {"name": "status", "full_type": "enum('Active','Inactive')"},
            ],
        },
        {
            "name": "salaries",
            "columns": [
                {"name": "employee_id"},
                {"name": "salary", "datatype": "decimal"},
            ],
        },
    ]
}


def _context(question, selected):
    return {"question": question, "selected_tables": selected}


def test_predicate_planner_erp_schema_under_repair_and_group():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("show equipment under repair in OE group", ["groups", "work_equipment"])
    manifest = planner.plan(context)
    assert manifest["status"] == "RESOLVED"
    filters = {(f["table"], f["column"], f["value"]) for f in manifest["filters"]}
    assert ("groups", "name", "OE") in filters
    assert any(f["table"] == "work_equipment" and f["column"] == "status" for f in manifest["filters"])


def test_predicate_planner_numeric_comparison():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("average salary above 90000", ["salaries"])
    manifest = planner.plan(context)
    assert manifest["status"] == "RESOLVED"
    filters = manifest["filters"]
    assert len(filters) == 1
    assert filters[0]["table"] == "salaries"
    assert filters[0]["column"] == "salary"
    assert filters[0]["operator"] == ">"
    assert filters[0]["value"] == 90000
    assert filters[0]["source"] == "Predicate Planner"


def test_predicate_planner_ambiguous_status_across_selected_tables():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("show active records", ["work_equipment", "work_orders"])
    manifest = planner.plan(context)
    assert manifest["status"] == "AMBIGUOUS"
    assert len(manifest["ambiguities"]) >= 1
    amb = manifest["ambiguities"][0]
    assert amb["value"] == "Active"
    assert len(amb["candidates"]) == 2


def test_predicate_planner_disambiguates_when_table_specified():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("show active work equipment", ["work_equipment", "work_orders"])
    manifest = planner.plan(context)
    assert manifest["status"] == "RESOLVED"
    assert len(manifest["filters"]) == 1
    assert manifest["filters"][0]["table"] == "work_equipment"


def test_predicate_planner_unknown_values_yields_empty_filters():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("show general items", ["groups"])
    manifest = planner.plan(context)
    assert manifest["status"] == "RESOLVED"
    assert len(manifest["filters"]) == 0
    assert len(manifest["ambiguities"]) == 0


def test_predicate_planner_structured_fields():
    planner = PredicatePlanner(ERP_SCHEMA)
    context = _context("show equipment under repair in OE group", ["groups", "work_equipment"])
    manifest = planner.plan(context)
    for f in manifest["filters"]:
        assert "table" in f
        assert "column" in f
        assert "operator" in f
        assert "value" in f
        assert "confidence" in f
        assert "reason" in f
        assert "source" in f
        assert "semantic_type" in f


def test_predicate_planner_prefers_observed_status_over_matching_class_token():
    class ObservedValues:
        def execute(self, query, params):
            return [{"value": "UNDER REPAIR"}] if "`status`" in query else [{"value": "Repair"}]

    resolver = ValueResolver(ERP_SCHEMA, database_adapter=ObservedValues())
    planner = PredicatePlanner(ERP_SCHEMA, value_resolver=resolver)
    manifest = planner.plan(_context("under repair in OE group", ["work_equipment", "groups"]))
    filters = {(item["table"], item["column"], item["value"], item["semantic_type"]) for item in manifest["filters"]}
    assert ("work_equipment", "status", "UNDER REPAIR", "STATUS") in filters
    assert ("groups", "name", "OE", "GROUP") in filters
    assert not any(item[1] in {"sus", "repair_class"} for item in filters)
