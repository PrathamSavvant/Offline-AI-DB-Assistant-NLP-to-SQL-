"""tests/unit/test_value_resolver.py

Unit tests for ValueResolver and ValueIndex.
"""

from core.value_resolver import ValueResolver, ValueIndex, ResolvedValue

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
            ],
        },
        {
            "name": "items",
            "columns": [
                {"name": "id"},
                {"name": "category", "full_type": "enum('Hardware','Software')"},
                {"name": "remarks", "datatype": "text"},
            ],
        },
    ]
}

EMPLOYEES_SCHEMA = {
    "tables": [
        {
            "name": "employees",
            "columns": [
                {"name": "id"},
                {"name": "name"},
                {"name": "department"},
                {"name": "status", "full_type": "enum('Active','On Leave','Terminated')"},
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


def test_value_index_lookup_matching():
    index = ValueIndex()
    index.add("Active", "Active", "employees", "status", 1.0, "enum definition")
    matches = index.lookup("Active")
    assert len(matches) == 1
    assert matches[0].table == "employees"
    assert matches[0].column == "status"
    assert matches[0].value == "Active"


def test_value_resolver_status_resolution():
    resolver = ValueResolver(ERP_SCHEMA)
    context = _context("show active groups", ["groups"])
    resolved = resolver.resolve(context)
    matching = [r for r in resolved if r.table == "groups" and r.column == "status"]
    assert len(matching) == 1
    assert matching[0].value == "Active"
    assert matching[0].confidence == 1.0


def test_value_resolver_category_resolution():
    resolver = ValueResolver(ERP_SCHEMA)
    context = _context("show items in Hardware category", ["items"])
    resolved = resolver.resolve(context)
    matching = [r for r in resolved if r.table == "items" and r.column == "category"]
    assert len(matching) == 1
    assert matching[0].value == "Hardware"


def test_value_resolver_group_lookup_resolution():
    resolver = ValueResolver(ERP_SCHEMA)
    context = _context("show equipment under repair in OE group", ["groups", "work_equipment"])
    resolved = resolver.resolve(context)
    group_matching = [r for r in resolved if r.table == "groups" and r.column == "name"]
    assert len(group_matching) == 1
    assert group_matching[0].value == "OE"


def test_value_resolver_under_repair_categorical_resolution():
    resolver = ValueResolver(ERP_SCHEMA)
    context = _context("show equipment under repair in OE group", ["groups", "work_equipment"])
    resolved = resolver.resolve(context)
    repair_matching = [r for r in resolved if r.table == "work_equipment" and r.column in {"sus", "repair_class"}]
    assert len(repair_matching) == 1
    assert repair_matching[0].semantic_type == "CATEGORY"


def test_value_resolver_unknown_values():
    resolver = ValueResolver(ERP_SCHEMA)
    context = _context("show unknown non_existent_thing", ["groups"])
    resolved = resolver.resolve(context)
    assert len(resolved) == 0


def test_value_resolver_excludes_text_and_remarks():
    resolver = ValueResolver(ERP_SCHEMA)
    col_obj = {"name": "remarks", "datatype": "text"}
    assert resolver._is_lookup_column("items", col_obj) is False


def test_value_resolver_employees_schema():
    resolver = ValueResolver(EMPLOYEES_SCHEMA)
    context = _context("list employees who are On Leave", ["employees"])
    resolved = resolver.resolve(context)
    matching = [r for r in resolved if r.table == "employees" and r.column == "status"]
    assert len(matching) == 1
    assert matching[0].value == "On Leave"
