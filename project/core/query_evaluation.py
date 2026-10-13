"""LLM-free, database-scoped acceptance checks for the deterministic agent."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable


def evaluate_case(builder: Any, case: Dict[str, Any]) -> Dict[str, Any]:
    """Run the real retrieval/planning boundary without composing or executing SQL."""
    started = time.perf_counter()
    context = builder.build(case["question"])
    plan = context.get("query_plan") or {}
    selected = list(context.get("selected_tables") or [])
    expected = set(case.get("expected_tables") or [])
    missing_tables = sorted(expected - set(selected))
    unexpected_tables = sorted(set(selected) - set(case["allowed_tables"])) if "allowed_tables" in case else []
    actual_predicates = plan.get("resolved_predicates") or []
    missing_predicates = []
    for predicate in case.get("expected_predicates") or []:
        if not any(all(actual.get(key) == value for key, value in predicate.items())
                   for actual in actual_predicates):
            # Report the location, not the potentially sensitive value.
            missing_predicates.append({"table": predicate.get("table"), "column": predicate.get("column")})
    join_plan = context.get("join_plan") or {}
    status = plan.get("status") or "missing"
    failures = []
    if status != case.get("expected_status", "ready"):
        failures.append(f"plan status {status}")
    if missing_tables:
        failures.append("expected tables not selected")
    if unexpected_tables:
        failures.append("unexpected tables selected")
    if missing_predicates:
        failures.append("expected predicates not resolved")
    if case.get("require_approved_joins", True) and len(expected) > 1 and not join_plan.get(
        "all_joins_approved", join_plan.get("all_joins_verified", False)
    ):
        failures.append("join path is not fully approved")
    if case.get("require_verified_joins", False) and len(expected) > 1 and not join_plan.get("all_joins_verified", False):
        failures.append("join path is not fully verified by MySQL foreign keys")
    if case.get("expected_join_tables"):
        actual_join_tables = set(join_plan.get("included_tables") or [])
        if not set(case["expected_join_tables"]) <= actual_join_tables:
            failures.append("expected join tables not included")
    ranked = [item.get("table") for item in context.get("scored_tables") or []]
    impact = context.get("knowledge_impact") or {}
    return {
        "id": case["id"],
        "passed": not failures,
        "failures": failures,
        "plan_status": status,
        "selected_tables": selected,
        "ranked_tables": ranked,
        "missing_tables": missing_tables,
        "unexpected_tables": unexpected_tables,
        "missing_predicates": missing_predicates,
        "resolved_predicate_count": len(actual_predicates),
        "join_verified": bool(join_plan.get("all_joins_verified", False)),
        "join_approved": bool(join_plan.get("all_joins_approved", join_plan.get("all_joins_verified", False))),
        "approved_non_fk_join_count": sum(
            edge.get("relationship_type") == "semantic" and edge.get("approved") is True
            for edge in join_plan.get("join_edges", [])
        ),
        "knowledge_effective": bool(impact.get("knowledge_effective")),
        "knowledge_entries_used": len(impact.get("knowledge_entries_used") or []),
        "knowledge_score_contribution": impact.get("knowledge_score_contribution", 0),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
    }


def run_suite(builder: Any, cases: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    results = [evaluate_case(builder, case) for case in cases]
    durations = sorted(item["elapsed_ms"] for item in results)
    return {
        "cases": results,
        "total": len(results),
        "passed": sum(item["passed"] for item in results),
        "failed": sum(not item["passed"] for item in results),
        "mean_ms": round(sum(item["elapsed_ms"] for item in results) / len(results), 2) if results else 0.0,
        "p95_ms": durations[max(0, (95 * len(durations) + 99) // 100 - 1)] if durations else 0.0,
        "max_ms": durations[-1] if durations else 0.0,
    }


def evaluate_full_case(engine: Any, case: Dict[str, Any]) -> Dict[str, Any]:
    """Optional live acceptance check; report only shape, not SQL or row data."""
    started = time.perf_counter()
    failures = []
    try:
        result = engine.execute(case["question"])
        model_decision = (getattr(engine, "last_prompt_info", None) or {}).get("llama_response") or {}
        executed = bool(result.get("sql"))
        expect_no_sql = bool(case.get("expect_no_sql"))
        if expect_no_sql and executed:
            failures.append("SQL executed when clarification or refusal was expected")
        elif not expect_no_sql and not executed:
            failures.append("no SQL executed")
        if not expect_no_sql and "expected_row_count" in case and result.get("row_count") != case["expected_row_count"]:
            failures.append("unexpected row count")
        if not expect_no_sql and "expected_columns" in case and not set(case["expected_columns"]) <= set(result.get("columns") or []):
            failures.append("expected columns missing")
        if not expect_no_sql and "expected_scalar" in case:
            rows = result.get("rows") or []
            if len(rows) != 1 or len(rows[0]) != 1 or next(iter(rows[0].values())) != case["expected_scalar"]:
                failures.append("expected scalar value did not match")
        if not expect_no_sql and "expected_rows" in case:
            actual_rows = result.get("rows") or []
            expected_rows = case["expected_rows"]
            canonical = lambda row: json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
            actual_values = [canonical(row) for row in actual_rows]
            expected_values = [canonical(row) for row in expected_rows]
            if case.get("require_row_order"):
                matches = actual_values == expected_values
            else:
                matches = sorted(actual_values) == sorted(expected_values)
            if not matches:
                failures.append("expected result rows did not match")
        return {"id": case["id"], "passed": not failures, "failures": failures,
                "sql_executed": executed,
                "model_confidence": model_decision.get("confidence"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as exc:
        return {"id": case["id"], "passed": False,
                "failures": [f"execution failed ({type(exc).__name__})"],
                "sql_executed": False,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}
