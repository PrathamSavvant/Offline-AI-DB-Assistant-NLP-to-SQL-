from core.query_guard import enforce_row_limit, is_safe_select
import pytest

@pytest.mark.parametrize("sql", ["SELECT * FROM employees", "WITH q AS (SELECT 1) SELECT * FROM q"])
def test_allows_read_only_sql(sql): assert is_safe_select(sql)[0]
@pytest.mark.parametrize("sql", ["DELETE FROM employees", "SELECT 1; DROP TABLE users", "", "UPDATE employees SET name='x'"])
def test_rejects_unsafe_sql(sql): assert not is_safe_select(sql)[0]
def test_adds_default_limit(): assert enforce_row_limit("SELECT * FROM employees").endswith("LIMIT 200;")
def test_preserves_existing_limit(): assert enforce_row_limit("SELECT * FROM employees LIMIT 3") == "SELECT * FROM employees LIMIT 3;"
def test_adds_limit_when_only_subquery_has_limit():
    sql = "SELECT * FROM (SELECT * FROM employees LIMIT 10) t"
    assert enforce_row_limit(sql).endswith("LIMIT 200;")

@pytest.mark.parametrize("comment", ["-- LIMIT 1\n", "# LIMIT 1\n", "/* LIMIT 1 */ "])
def test_limit_in_comment_does_not_suppress_outer_limit(comment):
    assert enforce_row_limit(comment + "SELECT * FROM employees").endswith("LIMIT 200;")
