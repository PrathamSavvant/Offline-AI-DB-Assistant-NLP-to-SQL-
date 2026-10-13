"""
config/importance_keywords.py

Editable list of keywords that signal a table is a "core" business entity,
used as one input to Phase 4 (Table Importance Ranking).

A table name containing any of these keywords (case-insensitive substring
match) gets a bonus toward its importance score — on top of its foreign
key connectivity and row count. This exists because a heavily-referenced
table like `orders` or `customers` is usually central to the business even
if its row count is modest, and connectivity alone doesn't always capture
that.

Edit this list freely based on what actually matters in your domain.
"""

IMPORTANCE_KEYWORDS = [
    "order", "transaction", "invoice", "customer", "employee", "product",
    "user", "account", "payment", "master", "equipment", "work_order",
    "project", "department",
]