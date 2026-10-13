import pytest

from services.sql_post_processor import (
    SQLPostProcessingError,
    SQLPostProcessor,
    SchemaReferenceValidator,
)


SCHEMA = {
    "tables": [
        {
            "name": "groups",
            "columns": [{"name": "id"}, {"name": "name"}, {"name": "status"}],
        },
        {
            "name": "work_equipment",
            "columns": [{"name": "id"}, {"name": "group_id"}, {"name": "status"}],
        },
    ]
}


class Dictionary:
    def expand_token(self, token):
        return {"SUS": "status"}.get(token.upper())


def test_reserved_table_is_escaped_only_when_it_exists_in_schema():
    sql = SQLPostProcessor(SCHEMA).process("SELECT groups.name FROM groups")
    assert "`groups`" in sql
    assert "`groups`.name" in sql


def test_schema_validator_reports_unknown_table_and_column():
    validator = SchemaReferenceValidator(SCHEMA)
    result = validator.validate("SELECT missing_column FROM unknown_table")
    assert result.valid is False
    assert result.missing_tables == ["unknown_table"]


def test_schema_validator_resolves_valid_aliases_and_rejects_invalid_ones():
    validator = SchemaReferenceValidator(SCHEMA)
    assert validator.validate("SELECT g.name FROM groups AS g").valid is True
    result = validator.validate("SELECT x.name FROM groups AS g")
    assert result.valid is False
    assert result.invalid_aliases == ["x"]


def test_case_correction_is_deterministic():
    sql = SQLPostProcessor(SCHEMA).process("SELECT GROUPS.NAME FROM GROUPS")
    assert "`groups`.name" in sql


def test_dictionary_approved_column_correction_is_applied():
    sql = SQLPostProcessor(SCHEMA, Dictionary()).process(
        "SELECT work_equipment.sus FROM work_equipment"
    )
    assert "work_equipment.status" in sql


def test_ambiguous_small_edit_correction_is_rejected():
    ambiguous_schema = {"tables": [{"name": "items", "columns": [{"name": "state"}, {"name": "status"}]}]}
    with pytest.raises(SQLPostProcessingError, match="Schema validation failed"):
        SQLPostProcessor(ambiguous_schema).process("SELECT items.statu FROM items")


def test_valid_query_passes_without_business_logic_changes():
    original = "SELECT work_equipment.status FROM work_equipment WHERE work_equipment.status = 1"
    processed = SQLPostProcessor(SCHEMA).process(original)
    assert "WHERE work_equipment.status = 1" in processed
