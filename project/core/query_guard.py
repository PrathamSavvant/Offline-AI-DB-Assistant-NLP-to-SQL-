"""
core/query_guard.py

Safety gate between LLM-generated SQL and actual database execution.

An LLM can hallucinate, misunderstand a question, or (rarely but
possibly) be manipulated into producing destructive SQL. This module
enforces a hard rule before anything reaches the database: only a single,
read-only SELECT (or SELECT-based CTE) statement is allowed through.
Nothing else is executed, no matter how the request was phrased upstream.
"""

import re
from typing import Tuple

# Keywords that should never appear in a query we're about to execute.
# Checked as whole words so e.g. "created" doesn't false-positive on "create".
FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "truncate", "create",
    "replace", "grant", "revoke", "exec", "execute", "call", "attach",
    "detach", "lock", "unlock", "load_file", "into outfile", "into dumpfile",
]

DEFAULT_ROW_LIMIT = 200


def _has_outer_limit(sql: str) -> bool:
    """Return whether a LIMIT belongs to the outermost SELECT statement."""
    depth = 0
    index = 0
    while index < len(sql):
        char = sql[index]
        if sql[index:index + 2] == "--":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if char == "#":
            newline = sql.find("\n", index + 1)
            index = len(sql) if newline == -1 else newline + 1
            continue
        if sql[index:index + 2] == "/*":
            comment_end = sql.find("*/", index + 2)
            index = len(sql) if comment_end == -1 else comment_end + 2
            continue
        if char in "'\"`":
            quote = char
            index += 1
            while index < len(sql):
                if sql[index] == "\\":
                    index += 2
                elif sql[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and sql[index:index + 5].lower() == "limit":
            before = sql[index - 1] if index else " "
            after = sql[index + 5] if index + 5 < len(sql) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
        index += 1
    return False


def is_safe_select(sql: str) -> Tuple[bool, str]:
    """
    Return (is_safe, reason). is_safe is True only if `sql` is a single
    SELECT (or WITH ... SELECT) statement containing none of the
    forbidden keywords and no stacked statements.
    """

    if not sql or not sql.strip():
        return False, "Generated SQL was empty."

    stripped = sql.strip().rstrip(";").strip()
    lowered = stripped.lower()

    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False, (
            "Only SELECT queries are allowed for safety. The generated "
            "query did not start with SELECT or WITH."
        )

    # A semicolon anywhere in the middle means stacked statements —
    # we already stripped a single trailing one above.
    if ";" in stripped:
        return False, "Multiple SQL statements detected; only one SELECT is allowed."

    for keyword in FORBIDDEN_KEYWORDS:
        pattern = r"\b" + re.escape(keyword) + r"\b" if keyword.isalpha() else re.escape(keyword)
        if re.search(pattern, lowered):
            return False, f"Query contains a disallowed keyword: '{keyword}'."

    return True, "OK"


def extract_referenced_tables(sql: str) -> set:
    """
    Extract every table name the query actually references, by scanning
    for identifiers following FROM or JOIN. CTE names (WITH x AS (...))
    are excluded, since they're query-local aliases, not real tables, and
    would otherwise be wrongly flagged as "not in the allowed list."

    This is intentionally a simple regex-based extractor, not a full SQL
    parser — it's good enough to catch the case this exists for: an LLM
    joining in a real table it was never shown, based on naming
    convention alone (e.g. seeing `department_id` and guessing there must
    be a `departments` table, then generating a correct join to it even
    though that table was excluded from its context).
    """

    cte_names = set(re.findall(r"\bWITH\s+([a-zA-Z_]\w*)\s+AS\s*\(", sql, re.IGNORECASE))
    cte_names |= set(re.findall(r",\s*([a-zA-Z_]\w*)\s+AS\s*\(", sql, re.IGNORECASE))
    cte_names_lower = {c.lower() for c in cte_names}

    referenced = re.findall(r"\b(?:FROM|JOIN)\s+`?([a-zA-Z_]\w*)`?", sql, re.IGNORECASE)

    return {t.lower() for t in referenced if t.lower() not in cte_names_lower}


def find_disallowed_tables(sql: str, allowed_tables) -> list:
    """
    Return any table referenced in `sql` that is NOT in `allowed_tables`
    (case-insensitive). This is what actually enforces "the LLM may only
    use the tables we showed it" — is_safe_select() only checks that the
    SQL *shape* is safe (a single SELECT), it says nothing about *which*
    tables ended up inside it. Without this check, a disabled table can
    still slip back in whenever the model guesses its name correctly from
    a foreign key column, and since the guessed table genuinely exists,
    MySQL executes it without complaint.
    """

    allowed_lower = {t.lower() for t in allowed_tables}
    referenced = extract_referenced_tables(sql)

    return sorted(t for t in referenced if t not in allowed_lower)


def enforce_row_limit(sql: str, limit: int = DEFAULT_ROW_LIMIT) -> str:
    """
    Append a LIMIT clause if the query doesn't already have one, so a
    poorly-scoped question can't return an unbounded result set into the
    browser. Assumes `sql` has already passed is_safe_select().
    """

    stripped = sql.strip().rstrip(";").strip()

    if _has_outer_limit(stripped):
        return stripped + ";"

    return f"{stripped} LIMIT {limit};"
