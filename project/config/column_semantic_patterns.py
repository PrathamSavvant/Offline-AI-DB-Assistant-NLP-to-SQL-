"""
config/column_semantic_patterns.py

Editable keyword patterns used by Phase 7 (Business Meaning Detection) to
tag what a column actually represents, based on its name.

Each semantic tag maps to a list of substrings checked against the column
name (case-insensitive). A column can match more than one tag (e.g.
"updated_at" matches "temporal", and "is_active" matches both "status" and
"boolean_flag").

Edit this file if you notice columns in your database that aren't being
tagged correctly, or that follow a different naming convention.
"""

from typing import Dict, List

COLUMN_SEMANTIC_PATTERNS: Dict[str, List[str]] = {
    "identifier": ["_id", "id", "uuid", "guid"],
    "temporal": ["date", "time", "_at", "created", "updated", "modified", "expiry", "expires"],
    "financial": ["price", "amount", "cost", "salary", "budget", "revenue", "payment", "fee", "balance"],
    "contact": ["email", "phone", "mobile", "address", "contact", "fax"],
    "status": ["status", "state", "stage", "approved", "rejected"],
    "boolean_flag": ["is_", "has_", "active", "enabled", "flag"],
    "descriptive": ["name", "title", "description", "comment", "note", "summary"],
    "quantity": ["count", "qty", "quantity", "total", "number", "num_"],
    "geographic": ["city", "state", "country", "zip", "postal", "location", "region"],
    "credential": ["password", "hash", "token", "secret", "api_key"],
}