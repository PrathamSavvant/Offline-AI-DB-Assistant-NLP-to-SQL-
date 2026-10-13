"""One policy for join edges admitted to SQL generation.

Database foreign keys remain structural truth. A human-approved semantic
relationship is separately usable, never mislabeled as a database FK.
"""
from __future__ import annotations

from typing import Any, Mapping


def is_approved_join_edge(edge: Mapping[str, Any]) -> bool:
    provenance = edge.get("provenance", "mysql_information_schema")
    if edge.get("verified") is True:
        return provenance == "mysql_information_schema"
    return (
        edge.get("verified") is False
        and edge.get("relationship_type") == "semantic"
        and provenance == "user_verified"
        and edge.get("approved") is True
        and bool(edge.get("constraint_name"))
    )
