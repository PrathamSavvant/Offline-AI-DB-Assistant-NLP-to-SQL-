from services.sql_reasoning_engine import SQLValidator, SQLValidationError
import pytest
def test_validator_limits_safe_query(): assert SQLValidator().validate("SELECT * FROM employees", {"selected_tables":["employees"]}).endswith("LIMIT 200;")
def test_validator_rejects_unknown_table():
    with pytest.raises(SQLValidationError): SQLValidator().validate("SELECT * FROM secrets", {"selected_tables":["employees"]})


def test_validator_rejects_stale_database_schema_prefix():
    with pytest.raises(SQLValidationError, match="active schema"):
        SQLValidator().validate("SELECT * FROM ai_sql_demo.employees", {"selected_tables": ["employees"]})
