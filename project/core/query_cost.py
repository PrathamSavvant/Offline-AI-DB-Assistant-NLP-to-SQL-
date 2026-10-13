"""Conservative, bounded MySQL EXPLAIN gate for generated SELECT queries."""
from __future__ import annotations

from typing import Any, Iterable, Mapping


class QueryCostError(ValueError):
    pass


def check_explain_plan(rows: Iterable[Mapping[str, Any]], max_scan_rows: int,
                       max_join_rows: int) -> dict:
    """Reject obviously expensive scans without interpreting result-set contents.

    Traditional EXPLAIN estimates are approximate. This is a guardrail, not a
    promise that MySQL will do exactly this much work.
    """
    plan = list(rows)
    if not plan:
        raise QueryCostError("MySQL returned no EXPLAIN plan; query was not executed.")
    scan_max = 0
    join_estimate = 1
    for item in plan:
        try:
            estimated = max(0, int(item.get("rows") or 0))
        except (TypeError, ValueError):
            raise QueryCostError("MySQL returned an unreadable row estimate; query was not executed.")
        scan_max = max(scan_max, estimated)
        if estimated:
            try:
                filtered = float(item.get("filtered") if item.get("filtered") is not None else 100)
            except (TypeError, ValueError):
                filtered = 100.0
            join_estimate *= max(1, int(estimated * max(0.0, min(filtered, 100.0)) / 100))
        if max_scan_rows > 0 and scan_max > max_scan_rows:
            raise QueryCostError("Estimated table scan exceeds the configured safety limit; query was not executed.")
        if max_join_rows > 0 and join_estimate > max_join_rows:
            raise QueryCostError("Estimated join work exceeds the configured safety limit; query was not executed.")
    return {"max_scan_rows": scan_max, "estimated_join_rows": join_estimate}
