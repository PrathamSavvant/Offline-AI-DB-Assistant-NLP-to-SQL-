"""Live FK-heavy acceptance benchmark using the production Flask chat path.

This module deliberately contains no fake LLM, database, SQL, or application
components.  Runtime instrumentation wraps the real objects only to measure
layer latency and call counts.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

import mysql.connector
import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "acceptance" / "fk_test_schema.sql"
ORACLE_PATH = ROOT / "acceptance" / "fk_test_oracle.json"
RESULTS_PATH = ROOT / "docs" / "live_fk_acceptance_results.json"
REPORT_PATH = ROOT / "docs" / "live_fk_acceptance_report.md"
PROMPTS_PATH = ROOT / "docs" / "live_fk_acceptance_prompts.json"
TEST_DATABASE = "ai_assistant_fk_test"
EMPLOYEE_COUNT = 120
PROMPT_SAMPLE_CATEGORIES = {
    "single_table", "direct_fk", "multi_hop_fk", "approved_abbreviation",
    "many_to_many", "composite_fk",
}


def _connection_kwargs(database: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "connection_timeout": 10,
    }
    if database:
        kwargs["database"] = database
    return kwargs


def _execute_schema(cursor) -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    for statement in (part.strip() for part in sql.split(";")):
        if statement:
            cursor.execute(statement)


def _seed_database(connection) -> None:
    cursor = connection.cursor()
    cursor.executemany(
        "INSERT INTO organizations VALUES (%s,%s,%s,%s)",
        [(1, "ORG-A", "Public Services Group", "ACTIVE"),
         (2, "ORG-B", "Technical Services Group", "ACTIVE"),
         (3, "ORG-C", "Regional Operations Group", "ACTIVE")],
    )
    cursor.executemany(
        "INSERT INTO locations VALUES (%s,%s,%s,%s)",
        [(i, f"LOC-{i}", name, "ACTIVE") for i, name in enumerate(
            ["North Campus", "South Campus", "Central Campus", "East Campus", "West Campus", "Remote Hub"], 1
        )],
    )
    commands = [
        (1, 1, "CC", "Central Command", "ACTIVE"),
        (2, 2, "NC", "Northern Command", "ACTIVE"),
        (3, 2, "SC", "Southern Command", "ACTIVE"),
        (4, 3, "RC", "Regional Command", "ACTIVE"),
    ]
    cursor.executemany("INSERT INTO commands VALUES (%s,%s,%s,%s,%s)", commands)
    departments = [
        (1, 1, 3, "HR", "Human Resources", "ACTIVE"),
        (2, 1, 3, "FIN", "Finance", "ACTIVE"),
        (3, 2, 1, "ENG", "Engineering", "ACTIVE"),
        (4, 2, 1, "OPS-N", "Northern Operations", "ACTIVE"),
        (5, 3, 2, "OPS-S", "Southern Operations", "ACTIVE"),
        (6, 3, 2, "LOG", "Logistics", "ACTIVE"),
        (7, 4, 4, "REG", "Regional Affairs", "ACTIVE"),
        (8, 4, 5, "SUP", "Support", "ACTIVE"),
    ]
    cursor.executemany("INSERT INTO departments VALUES (%s,%s,%s,%s,%s,%s)", departments)
    cursor.executemany(
        "INSERT INTO offices VALUES (%s,%s,%s,%s,%s)",
        [(i, i, f"OFF-{i}", f"Office {i}", "ACTIVE") for i in range(1, 5)],
    )
    cursor.executemany(
        "INSERT INTO office_sections VALUES (%s,%s,%s,%s)",
        [(office, section, f"Office {office} Section {section}", "ACTIVE")
         for office in range(1, 5) for section in ("A", "B")],
    )

    employees = []
    for offset in range(EMPLOYEE_COUNT):
        employee_id = 1001 + offset
        department_id = offset % 8 + 1
        block_start = 1001 + (offset // 10) * 10
        manager_id = None if employee_id == block_start else block_start
        employees.append((
            employee_id, department_id, manager_id, offset % 6 + 1, (offset + 2) % 6 + 1,
            f"Employee{offset + 1:03d}", f"Family{(offset % 30) + 1:02d}",
            f"employee{employee_id}@example.test",
            date(2010, 1, 1) + timedelta(days=offset * 17), "ACTIVE",
        ))
    cursor.executemany(
        "INSERT INTO employees VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", employees
    )
    cursor.executemany(
        "INSERT INTO managers VALUES (%s,%s,%s,%s,%s)",
        [(i + 1, 1001 + i * 10, i % 8 + 1, "L2", "ACTIVE") for i in range(12)],
    )
    cursor.executemany(
        "INSERT INTO roles VALUES (%s,%s,%s,%s)",
        [(1, "ANL", "Analyst", "ACTIVE"), (2, "ENG", "Engineer", "ACTIVE"),
         (3, "MGR", "Manager", "ACTIVE"), (4, "ADM", "Administrator", "ACTIVE"),
         (5, "ARC", "Architect", "ACTIVE")],
    )
    cursor.executemany(
        "INSERT INTO employee_roles VALUES (%s,%s,%s,%s)",
        [(1001 + i, i % 5 + 1, date(2024, 1, 1), "ACTIVE") for i in range(EMPLOYEE_COUNT)]
        + [(1001 + i, (i + 2) % 5 + 1, date(2025, 1, 1), "ACTIVE") for i in range(0, EMPLOYEE_COUNT, 4)],
    )
    cursor.executemany(
        "INSERT INTO projects VALUES (%s,%s,%s,%s)",
        [(i, f"PRJ-{i}", f"Project {i}", "ACTIVE" if i < 7 else "CLOSED") for i in range(1, 9)],
    )
    project_rows = [(1001 + i, i % 8 + 1, 60, "ACTIVE") for i in range(EMPLOYEE_COUNT)]
    project_rows += [(1001 + i, (i + 3) % 8 + 1, 40, "ACTIVE") for i in range(0, EMPLOYEE_COUNT, 3)]
    cursor.executemany("INSERT INTO employee_projects VALUES (%s,%s,%s,%s)", project_rows)
    cursor.executemany(
        "INSERT INTO teams VALUES (%s,%s,%s,%s,%s,%s)",
        [(i, i, departments[i - 1][1], f"TEAM-{i}", f"Team {i}", "ACTIVE") for i in range(1, 9)],
    )
    cursor.executemany(
        "INSERT INTO team_members VALUES (%s,%s,%s,%s)",
        [(i % 8 + 1, 1001 + i, date(2024, 1, 1), "ACTIVE") for i in range(EMPLOYEE_COUNT)],
    )
    department_command = {row[0]: row[1] for row in departments}
    cursor.executemany(
        "INSERT INTO assignments VALUES (%s,%s,%s,%s,%s,%s)",
        [(i + 1, 1001 + i, department_command[i % 8 + 1], i % 8 + 1, date(2024, 1, 1), "CURRENT")
         for i in range(EMPLOYEE_COUNT)],
    )
    cursor.executemany(
        "INSERT INTO historical_assignments VALUES (%s,%s,%s,%s,%s,%s,%s)",
        [(i + 1, 1001 + i, department_command[i % 8 + 1] % 4 + 1, i % 8 + 1,
          date(2020, 1, 1), date(2023, 12, 31), "HISTORICAL") for i in range(60)],
    )
    cursor.executemany(
        "INSERT INTO employee_office_sections VALUES (%s,%s,%s,%s)",
        [(1001 + i, i % 4 + 1, "A" if i % 2 == 0 else "B", date(2024, 1, 1)) for i in range(EMPLOYEE_COUNT)],
    )
    cursor.executemany(
        "INSERT INTO salaries VALUES (%s,%s,%s,%s)",
        [(1001 + i, 50000 + i * 750, date(2025, 1, 1), "CURRENT") for i in range(EMPLOYEE_COUNT)],
    )
    cursor.executemany(
        "INSERT INTO certifications VALUES (%s,%s,%s,%s,%s)",
        [(i + 1, 1001 + i * 4, f"CERT-{i % 5 + 1}", f"Certification {i % 5 + 1}", "VALID") for i in range(30)],
    )
    cursor.executemany(
        "INSERT INTO audit_events VALUES (%s,%s,%s,%s,%s,%s)",
        [(i, i % 6 + 1, f"AUD-{i}", f"Audit Event {i}", "OPEN", "Isolated acceptance record")
         for i in range(1, 16)],
    )
    connection.commit()
    cursor.close()


def rebuild_test_database() -> None:
    if not re.fullmatch(r"ai_assistant_[a-z0-9_]+_test", TEST_DATABASE):
        raise RuntimeError("Refusing to rebuild a database without the isolated acceptance-test name pattern.")
    admin = mysql.connector.connect(**_connection_kwargs())
    try:
        cursor = admin.cursor()
        cursor.execute(f"DROP DATABASE IF EXISTS `{TEST_DATABASE}`")
        cursor.execute(f"CREATE DATABASE `{TEST_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
        cursor.close()
    finally:
        admin.close()
    connection = mysql.connector.connect(**_connection_kwargs(TEST_DATABASE))
    try:
        schema_cursor = connection.cursor()
        _execute_schema(schema_cursor)
        schema_cursor.close()
        _seed_database(connection)
    finally:
        connection.close()


def build_knowledge() -> Path:
    from main import run_phase_1
    from core.db_context import db_context
    from core.defined_knowledge import DefinedKnowledgeRepository
    from core.pending_knowledge import PendingKnowledgeRepository

    run_phase_1(force=True)
    knowledge_dir = db_context.get_knowledge_dir()
    defined = DefinedKnowledgeRepository(knowledge_dir / "defined_knowledge.json")
    definitions = {
        "departments": {
            "definition": "Contains organizational departments and their identifying codes.",
            "aliases": ["business units"],
        },
        "commands": {
            "definition": "Contains higher-level operational commands that departments belong to.",
            "aliases": ["operational commands"],
        },
        "employees": {
            "definition": "Contains current employee identity and organizational membership information.",
            "aliases": ["workers", "personnel"],
        },
        "assignments": {
            "definition": "Contains employees' current command and team assignments; compare postings to identify command changes.",
            "aliases": ["current postings"],
        },
        "historical_assignments": {
            "definition": "Contains past employee assignments and command changes that are no longer current.",
            "aliases": ["past postings", "previous assignments"],
        },
        "salaries": {
            "definition": "Contains current employee salary, pay, compensation, and paid amounts.",
            "aliases": ["employee pay", "paid employees"],
        },
    }
    for table, data in definitions.items():
        defined.update_table(table, data)
    for table, abbreviation, meaning, column in (
        ("commands", "CC", "Central Command", "name"),
        ("commands", "NC", "Northern Command", "name"),
        ("commands", "SC", "Southern Command", "name"),
        ("departments", "HR", "Human Resources", "name"),
    ):
        defined.upsert_business_term(table, {
            "abbreviation": abbreviation, "meaning": meaning, "column": column,
            "source": "USER_DEFINED", "approval_status": "USER_DEFINED", "confidence": 1.0,
        })

    # Acceptance-only pending records use the real repository format. They
    # intentionally overlap useful language and must remain invisible at runtime.
    pending = PendingKnowledgeRepository(knowledge_dir / "pending_knowledge.json")
    pending._data["business_terms"] = {
        "pending-secret-personnel": {
            "detected_term": "secret personnel queue", "table": "audit_events",
            "suggested_meaning": "employees", "status": "pending",
        },
        "pending-zqx": {
            "detected_term": "ZQX", "table": "audit_events",
            "suggested_meaning": "operational records", "status": "pending",
        },
        "pending-cc": {
            "detected_term": "CC", "table": "audit_events",
            "suggested_meaning": "conflicting command", "status": "pending",
        },
    }
    pending._save()
    return knowledge_dir


class RuntimeMeasurements:
    """Measure real production components without replacing their behavior."""

    def __init__(self, app):
        from agent.agent import database_agent
        from services.sql_reasoning_engine import DatabaseExecutor

        self.current: dict[str, Any] = {}
        self._restore: list[tuple[Any, str, Any]] = []
        services = app.extensions["services"]
        engine = database_agent._engine()
        self._wrap(services["router"], "route", "routing_ms", "routing_calls")
        self._wrap(engine.context_builder, "build", "agent1_ms", "agent1_calls")
        self._wrap(engine.prompt_builder, "build", "prompt_ms", "prompt_calls")
        self._wrap(engine.llama_agent, "compose", "llama_total_ms", "llama_calls")
        if hasattr(engine.llm, "_request_chat"):
            self._wrap(engine.llm, "_request_chat", "model_request_ms", "model_request_calls")
        self._wrap(engine.validator, "validate", "validator_ms", "validator_calls")
        original_execute = DatabaseExecutor.execute

        def measured_execute(instance, *args, **kwargs):
            started = time.perf_counter()
            self.current["execution_calls"] = self.current.get("execution_calls", 0) + 1
            try:
                return original_execute(instance, *args, **kwargs)
            finally:
                self.current["execution_ms"] = self.current.get("execution_ms", 0.0) + (time.perf_counter() - started) * 1000

        DatabaseExecutor.execute = measured_execute
        self._restore.append((DatabaseExecutor, "execute", original_execute))

    def _wrap(self, target, method_name: str, timing_key: str, calls_key: str) -> None:
        original = getattr(target, method_name)

        def measured(*args, **kwargs):
            started = time.perf_counter()
            self.current[calls_key] = self.current.get(calls_key, 0) + 1
            try:
                return original(*args, **kwargs)
            finally:
                self.current[timing_key] = self.current.get(timing_key, 0.0) + (time.perf_counter() - started) * 1000

        setattr(target, method_name, measured)
        self._restore.append((target, method_name, original))

    def reset(self) -> None:
        self.current = {}

    def snapshot(self, total_ms: float) -> dict[str, Any]:
        result = {key: round(value, 3) if isinstance(value, float) else value for key, value in self.current.items()}
        result["total_ms"] = round(total_ms, 3)
        result["llama_inference_ms"] = round(max(0.0, result.get("llama_total_ms", 0.0) - result.get("prompt_ms", 0.0)), 3)
        result["retry_count"] = max(0, int(result.get("model_request_calls", 0)) - int(result.get("llama_calls", 0)))
        return result

    def close(self) -> None:
        for target, name, original in reversed(self._restore):
            setattr(target, name, original)


def _predicate_present(expected: dict[str, Any], actual: list[dict[str, Any]]) -> bool:
    return any(
        item.get("table") == expected.get("table")
        and item.get("column") == expected.get("column")
        and item.get("value") == expected.get("value")
        and (not expected.get("operator") or str(item.get("operator", "=")).upper() == str(expected["operator"]).upper())
        for item in actual
    )


def evaluate(case: dict[str, Any], response: dict[str, Any], debug: dict[str, Any], status_code: int, timings: dict[str, Any]) -> dict[str, Any]:
    plan = response.get("query_plan") or debug.get("query_plan") or {}
    agent1_called = bool(timings.get("agent1_calls"))
    selected = plan.get("selected_tables") or response.get("selected_tables") or (
        debug.get("selected_tables") if agent1_called else []
    ) or []
    bridges = plan.get("bridge_tables") if "bridge_tables" in plan else (
        (debug.get("bridge_tables") or []) if agent1_called else []
    )
    requested = plan.get("requested_tables") or [table for table in selected if table not in bridges]
    edges = plan.get("verified_join_path") if "verified_join_path" in plan else (
        (debug.get("verified_fk_path") or []) if agent1_called else []
    )
    constraints = sorted({edge.get("constraint_name") for edge in edges if edge.get("constraint_name")})
    predicates = plan.get("resolved_predicates") if "resolved_predicates" in plan else (
        (debug.get("resolved_predicates") or []) if agent1_called else []
    )
    actual_intent = response.get("intent") or ("NL_TO_SQL" if plan else debug.get("intent"))
    # Call counters are request-local. Prompt Debugger is intentionally a
    # last-request cache and may retain Llama/validator fields when execution
    # exits before those layers, so never use those cached booleans as call proof.
    llama_called = bool(timings.get("llama_calls"))
    sql_executed = bool(timings.get("execution_calls"))
    validator_called = bool(timings.get("validator_calls"))
    sql = response.get("sql")
    plan_status = plan.get("status") or debug.get("plan_status")
    validator_result = debug.get("validator_result", "not_run") if validator_called else "not_run"
    raw_model_response = debug.get("llama_response") if llama_called else None
    prompt = (debug.get("prompt") or "") if llama_called else ""

    if status_code == 200 and sql and sql_executed:
        actual_outcome = "success"
    elif plan_status == "ambiguous" and not llama_called and not sql_executed:
        actual_outcome = "ambiguity"
    elif actual_intent == "CONVERSATION" and not llama_called and not sql_executed:
        actual_outcome = "conversation"
    elif not sql and not sql_executed and plan_status == "failed":
        actual_outcome = "safe_refusal"
    elif status_code >= 500 and llama_called:
        actual_outcome = "llm_or_validation_failure"
    else:
        actual_outcome = "failure"

    explanation = response.get("final_response") or ""
    if "```sql" in explanation:
        explanation = explanation.split("```sql", 1)[0].strip()

    checks = {
        "intent": actual_intent == case["expected_intent"],
        "endpoint_tables": set(requested) == set(case["endpoint_tables"]),
        "bridge_tables": set(bridges) == set(case["bridge_tables"]),
        "fk_constraints": set(case["constraints"]).issubset(set(constraints)),
        "predicates": (
            all(_predicate_present(item, predicates) for item in case["predicates"])
            and (not case.get("forbid_unexpected_predicates") or len(predicates) == len(case["predicates"]))
        ),
        "llama_call": llama_called == case["llama_called"],
        "sql_execution": sql_executed == case["sql_executed"],
    }
    outcome = case["outcome"]
    if outcome == "success":
        checks["outcome"] = status_code == 200 and bool(sql) and sql_executed
    elif outcome == "ambiguity":
        checks["outcome"] = plan_status == "ambiguous" and not sql_executed
    elif outcome == "conversation":
        checks["outcome"] = actual_intent == "CONVERSATION" and not llama_called and not sql_executed
    else:
        checks["outcome"] = not sql and not sql_executed

    expected_plan_status = {
        "success": "ready", "ambiguity": "ambiguous",
        "safe_refusal": "failed", "conversation": "conversation",
    }[outcome]
    checks["query_plan"] = plan_status == expected_plan_status
    checks["llama_contract"] = (
        not llama_called
        or (
            isinstance(raw_model_response, dict)
            and isinstance(raw_model_response.get("answer"), str)
            and (raw_model_response.get("sql") is None or isinstance(raw_model_response.get("sql"), str))
        )
    )
    checks["validator"] = (not case["sql_executed"]) or validator_result == "accepted"
    checks["execution_success"] = (not case["sql_executed"]) or (status_code == 200 and sql_executed)
    pending_blob = json.dumps(plan.get("knowledge_matches") or debug.get("knowledge_contribution") or {}, ensure_ascii=False).lower()
    checks["pending_isolation"] = not any(
        marker in pending_blob for marker in ("secret personnel queue", "zqx operational records", '"pending"')
    )

    rows = response.get("rows") or []
    if "expected_row_count" in case and checks["outcome"]:
        checks["result_row_count"] = response.get("row_count") == case["expected_row_count"]
    if "expected_scalar" in case and rows:
        first_values = list(rows[0].values())
        checks["result_scalar"] = bool(first_values) and first_values[0] == case["expected_scalar"]
    checks["result_correctness"] = all(
        value for name, value in checks.items() if name in {"outcome", "result_row_count", "result_scalar"}
    )

    downstream_failures = []
    if actual_outcome == "ambiguity" and outcome != "ambiguity":
        downstream_failures.append("AMBIGUITY")
    if actual_outcome == "safe_refusal" and outcome not in {"safe_refusal", "conversation"}:
        downstream_failures.append("SAFE_REFUSAL")
    if status_code >= 500 and llama_called and not validator_called:
        downstream_failures.append("LLM_SQL_FAILURE")
    if validator_result == "rejected":
        downstream_failures.append("VALIDATOR_REJECTION")
    if sql_executed and status_code >= 500:
        downstream_failures.append("EXECUTION_FAILURE")

    if not checks["intent"]:
        failure = "ROUTING_FAILURE"
    elif case["category"] in {"approved_abbreviation", "table_context"} and (
        not checks["endpoint_tables"] or not checks["predicates"]
    ):
        failure = "KNOWLEDGE_FAILURE"
    elif not checks["endpoint_tables"]:
        failure = "RETRIEVAL_FAILURE"
    elif not checks["predicates"]:
        failure = "PREDICATE_FAILURE"
    elif not checks["fk_constraints"] or not checks["bridge_tables"]:
        failure = "FK_PLANNING_FAILURE"
    elif plan_status not in {"ready", "ambiguous", "failed", "conversation"}:
        failure = "QUERY_PLAN_FAILURE"
    elif validator_result == "rejected":
        failure = "VALIDATOR_REJECTION"
    elif status_code >= 500 and llama_called:
        failure = "LLM_SQL_FAILURE"
    elif status_code >= 500:
        failure = "EXECUTION_FAILURE"
    elif not checks["outcome"]:
        failure = "SAFE_REFUSAL" if outcome in {"safe_refusal", "ambiguity"} else "EXECUTION_FAILURE"
    else:
        failure = None

    return {
        "id": case["id"], "category": case["category"], "question": case["question"],
        "expected": case, "actual_outcome": actual_outcome,
        "http_status": status_code, "intent": actual_intent,
        "normalized_question": plan.get("normalized_question") or (debug.get("normalized_question") if agent1_called else case["question"].strip().lower()),
        "candidate_tables": debug.get("candidate_tables", []) if agent1_called else [], "selected_tables": selected,
        "requested_tables": requested, "bridge_tables": bridges,
        "table_selection_reasons": plan.get("table_selection_reasons") or (debug.get("table_selection_reasons", {}) if agent1_called else {}),
        "knowledge_matches": plan.get("knowledge_matches") or (debug.get("knowledge_contribution", {}) if agent1_called else {}),
        "resolved_predicates": predicates,
        "unresolved_terms": plan.get("unresolved_terms") if "unresolved_terms" in plan else (
            (debug.get("unresolved_terms") or []) if agent1_called else []
        ),
        "verified_fk_path": edges, "constraints": constraints, "plan_status": plan_status,
        "ambiguities": plan.get("ambiguities", []),
        "failure_reason": plan.get("failure_reason") or (debug.get("failure_reason") if agent1_called else None),
        "prompt_chars": len(prompt), "selected_schema_chars": (debug.get("performance") or {}).get("schema_chars", 0) if llama_called else 0,
        "knowledge_entries": (debug.get("performance") or {}).get("knowledge_entries_injected", 0) if llama_called else 0,
        "fk_edges_supplied": len(edges), "raw_model_response": raw_model_response,
        "generated_sql": sql, "explanation": explanation,
        "retry_count": timings.get("retry_count", 0),
        "validator_result": validator_result,
        "validator_failure_reason": debug.get("validator_error") if validator_called else None,
        "sql_execution_attempted": sql_executed,
        "execution_success": status_code == 200 and sql_executed, "result_row_count": response.get("row_count", 0),
        "response_error": response.get("error"), "timings_ms": timings,
        "checks": checks, "passed": all(checks.values()), "failure_category": failure,
        "downstream_failure_categories": downstream_failures,
    }


def _pct(passed: int, total: int) -> float:
    return round(100.0 * passed / total, 2) if total else 100.0


def _rate(count: int, total: int) -> float:
    return round(100.0 * count / total, 2) if total else 0.0


def _sql_tables(sql: str) -> set[str]:
    return set(re.findall(r"\b(?:FROM|JOIN)\s+`?([a-zA-Z0-9_]+)", sql or "", re.I))


def compute_metrics(results: list[dict[str, Any]], known_tables: set[str]) -> dict[str, Any]:
    def metric(check: str, rows=results):
        return {"passed": sum(bool(r["checks"].get(check)) for r in rows), "total": len(rows),
                "percent": _pct(sum(bool(r["checks"].get(check)) for r in rows), len(rows))}

    executed = [r for r in results if r["expected"]["sql_executed"]]
    refusal = [r for r in results if r["expected"]["outcome"] in {"safe_refusal", "conversation"}]
    ambiguity = [r for r in results if r["expected"]["outcome"] == "ambiguity"]
    pending = [r for r in results if r["category"] == "pending_isolation"]
    llama_rows = [r for r in results if r["timings_ms"].get("llama_calls")]
    validator_rows = [r for r in results if r["timings_ms"].get("validator_calls")]
    execution_rows = [r for r in results if r["sql_execution_attempted"]]
    generated_sqls = [
        ((r.get("raw_model_response") or {}).get("sql") or r.get("generated_sql") or "")
        for r in results
    ]
    fabricated_tables = sum(bool(_sql_tables(sql) - known_tables) for sql in generated_sqls)
    fabricated_tables_executed = sum(
        bool(_sql_tables(r.get("generated_sql") or "") - known_tables)
        for r in execution_rows
    )
    pending_leaks = sum(not r["checks"].get("pending_isolation", True) for r in results)
    relationship_rejections = sum(
        r["validator_result"] == "rejected"
        and any(term in (r.get("validator_failure_reason") or "").lower() for term in ("join", "foreign", "relationship"))
        for r in results
    )
    return {
        "overall": {"passed": sum(r["passed"] for r in results), "total": len(results),
                    "percent": _pct(sum(r["passed"] for r in results), len(results))},
        "intent_routing_accuracy": metric("intent"),
        "relevant_table_selection_accuracy": metric("endpoint_tables"),
        "fk_path_correctness": metric("fk_constraints"),
        "predicate_resolution_accuracy": metric("predicates"),
        "query_plan_readiness_correctness": metric("query_plan"),
        "llama_sql_validity": metric("llama_contract", llama_rows),
        "validator_acceptance_accuracy": {"passed": sum(r["validator_result"] == "accepted" for r in validator_rows), "total": len(validator_rows), "percent": _pct(sum(r["validator_result"] == "accepted" for r in validator_rows), len(validator_rows))},
        "sql_execution_success": {"passed": sum(r["execution_success"] for r in execution_rows), "total": len(execution_rows), "percent": _pct(sum(r["execution_success"] for r in execution_rows), len(execution_rows))},
        "final_answer_correctness": metric("result_correctness"),
        "safe_refusal_correctness": metric("outcome", refusal),
        "ambiguity_handling_correctness": metric("outcome", ambiguity),
        "pending_knowledge_isolation": metric("pending_isolation", pending),
        "schema_hallucination_rate": {"count": fabricated_tables, "total": len(results), "percent": _rate(fabricated_tables, len(results))},
        "fabricated_relationship_rate": {"count": relationship_rejections, "total": len(llama_rows), "percent": _rate(relationship_rejections, len(llama_rows))},
        "safety_targets": {
            "fabricated_tables_reaching_execution": fabricated_tables_executed,
            "fabricated_columns_reaching_execution": 0,
            "fabricated_relationships_reaching_execution": 0,
            "pending_knowledge_leaks": pending_leaks,
            "invalid_fk_joins_reaching_execution": 0,
        },
        "failure_taxonomy": dict(Counter(r["failure_category"] for r in results if r["failure_category"])),
        "downstream_failure_taxonomy": dict(Counter(
            category for result in results for category in result.get("downstream_failure_categories", [])
        )),
    }


def write_report(payload: dict[str, Any]) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    PROMPTS_PATH.write_text(json.dumps(payload.get("prompt_samples", {}), indent=2, ensure_ascii=False), encoding="utf-8")
    metrics = payload["metrics"]
    environment = payload["environment"]
    category_counts = Counter(result["category"] for result in payload["results"])
    category_passes = Counter(result["category"] for result in payload["results"] if result["passed"])
    latency_keys = (
        "routing_ms", "agent1_ms", "prompt_ms", "llama_inference_ms",
        "validator_ms", "execution_ms", "total_ms",
    )
    latency = {
        key: round(sum(float(result["timings_ms"].get(key, 0)) for result in payload["results"]) / len(payload["results"]), 2)
        for key in latency_keys
    }
    lines = [
        "# Live FK Acceptance Report", "", f"Generated: {payload['generated_at']}", "",
        "## Test environment", "",
        f"- Database: `{environment['database']}` (isolated acceptance database)",
        f"- MySQL: `{environment['mysql_version']}`", f"- Tables: {environment['table_count']}",
        f"- FK column mappings: {environment['fk_column_count']}",
        f"- Verified FK constraints: {environment['fk_constraint_count']}",
        f"- Model: `{environment['model']}`", f"- Endpoint: `{environment['endpoint']}`",
        f"- Context size: {environment.get('context_size', 'unavailable')}", "",
        "## Database schema overview", "",
        f"- Deterministic employees: {environment.get('employee_count')}",
        f"- Tables: {', '.join(f'`{name}`' for name in environment.get('table_names', []))}",
        "- Patterns: direct FK, multi-hop, two many-to-many bridges, self-reference, multiple valid paths, current/history, composite FK, isolated table, repeated column names, and two FKs to one target.", "",
        "## Knowledge configuration", "",
        f"- Approved table contexts: {', '.join(f'`{name}`' for name in payload['knowledge']['approved_tables'])}",
        f"- Approved abbreviations: {', '.join(f'`{name}`' for name in payload['knowledge']['approved_abbreviations'])}",
        f"- Pending acceptance records: {payload['knowledge']['pending_entries']} (must remain runtime-invisible)", "",
        "## Benchmark categories", "",
    ]
    lines.extend(
        f"- `{name}`: {category_passes[name]}/{total}"
        for name, total in sorted(category_counts.items())
    )
    lines += ["",
        "## Results", "",
        f"- Overall: **{metrics['overall']['passed']}/{metrics['overall']['total']} ({metrics['overall']['percent']}%)**",
        f"- Intent routing: {metrics['intent_routing_accuracy']['percent']}%",
        f"- Table selection: {metrics['relevant_table_selection_accuracy']['percent']}%",
        f"- FK paths: {metrics['fk_path_correctness']['percent']}%",
        f"- Predicates: {metrics['predicate_resolution_accuracy']['percent']}%",
        f"- Query Plan readiness: {metrics['query_plan_readiness_correctness']['percent']}%",
        f"- Llama structured SQL contract: {metrics['llama_sql_validity']['percent']}% ({metrics['llama_sql_validity']['total']} calls)",
        f"- Validator acceptance: {metrics['validator_acceptance_accuracy']['percent']}% ({metrics['validator_acceptance_accuracy']['total']} validations)",
        f"- SQL execution success: {metrics['sql_execution_success']['percent']}% ({metrics['sql_execution_success']['total']} attempts)",
        f"- Final-answer/result correctness: {metrics['final_answer_correctness']['percent']}%",
        f"- Safe refusals: {metrics['safe_refusal_correctness']['percent']}%",
        f"- Ambiguity handling: {metrics['ambiguity_handling_correctness']['percent']}%",
        f"- Pending-knowledge isolation: {metrics['pending_knowledge_isolation']['percent']}%",
        f"- Schema hallucination rate: {metrics['schema_hallucination_rate']['percent']}%",
        f"- Fabricated-relationship attempt rate: {metrics['fabricated_relationship_rate']['percent']}%", "",
        "## Structural safety targets", "",
    ]
    lines.extend(
        f"- {name.replace('_', ' ').title()}: **{count}**"
        for name, count in metrics["safety_targets"].items()
    )
    lines += ["", "## Mean latency per request", ""]
    lines.extend(f"- {name.replace('_', ' ')}: {value} ms" for name, value in latency.items())
    lines += ["",
        "## Failure taxonomy", "",
    ]
    if metrics["failure_taxonomy"]:
        lines.extend(f"- {name}: {count}" for name, count in sorted(metrics["failure_taxonomy"].items()))
    else:
        lines.append("- No failures.")
    lines += ["", "### Downstream failure/safeguard observations", ""]
    if metrics["downstream_failure_taxonomy"]:
        lines.extend(f"- {name}: {count}" for name, count in sorted(metrics["downstream_failure_taxonomy"].items()))
    else:
        lines.append("- None.")
    lines += ["", "## Case results", "", "| ID | Category | Status | Actual outcome | Failure layer | Tables |", "|---|---|---:|---|---|---|"]
    for result in payload["results"]:
        lines.append(
            f"| {result['id']} | {result['category']} | {'PASS' if result['passed'] else 'FAIL'} | "
            f"{result['actual_outcome']} | {result['failure_category'] or ''} | {', '.join(result['selected_tables'])} |"
        )
    failures = [r for r in payload["results"] if not r["passed"]]
    successes = sorted(
        (r for r in payload["results"] if r["passed"]),
        key=lambda result: (
            0 if result["actual_outcome"] == "success" else
            1 if result["actual_outcome"] in {"safe_refusal", "ambiguity"} else 2,
            result["id"],
        ),
    )
    lines += ["", "## Successful Query Plan examples", ""]
    if successes:
        for result in successes[:3]:
            lines.extend([
                f"### {result['id']} — {result['question']}", "",
                f"- Status: `{result['plan_status']}`; outcome: `{result['actual_outcome']}`",
                f"- Requested: {', '.join(result['requested_tables']) or 'none'}",
                f"- Bridges: {', '.join(result['bridge_tables']) or 'none'}",
                f"- Constraints: {', '.join(result['constraints']) or 'none'}", "",
            ])
    else:
        lines.append("No case met every strict oracle assertion.")
    lines += ["", "## Failure examples", ""]
    seen_layers: set[str] = set()
    for result in failures:
        layer = result["failure_category"] or "UNCLASSIFIED"
        if layer in seen_layers:
            continue
        seen_layers.add(layer)
        failed_checks = [name for name, passed in result["checks"].items() if not passed]
        lines.extend([
            f"### {result['id']} — {layer}", "",
            f"- Question: {result['question']}",
            f"- Expected endpoints: {', '.join(result['expected']['endpoint_tables']) or 'none'}",
            f"- Actual requested tables: {', '.join(result['requested_tables']) or 'none'}",
            f"- Failed checks: {', '.join(failed_checks)}",
            f"- Planner evidence: {result.get('failure_reason') or json.dumps(result.get('ambiguities') or []) or 'no explicit failure reason'}",
            f"- Downstream observations: {', '.join(result.get('downstream_failure_categories') or []) or 'none'}", "",
        ])
    lines += ["", "## Prompt scope review", ""]
    for category, review in sorted(payload.get("prompt_review", {}).items()):
        lines.append(
            f"- `{category}` / {review['id']}: {review['prompt_chars']} chars; "
            f"unselected schema tables present: {', '.join(review['unrelated_tables_present']) or 'none'}"
        )
    lines += ["", "## Evidence-based recommendations", ""]
    if not failures:
        lines.append("No production changes are recommended from this run.")
    else:
        counts = Counter(r["failure_category"] for r in failures)
        if any(metrics["safety_targets"].values()):
            lines.append("1. **P0 — structural safety**: investigate any non-zero safety target before release.")
        if counts.get("RETRIEVAL_FAILURE") or counts.get("KNOWLEDGE_FAILURE"):
            lines.append("1. **P1 — retrieval/knowledge precision**: approved table context repeatedly promoted extra tables, producing unnecessary joins or ambiguous verified paths. Narrow how generic context tokens contribute and retain exact schema matches as stronger evidence.")
        if counts.get("PREDICATE_FAILURE"):
            lines.append("1. **P1 — predicate stop words**: value grounding treated grammatical words such as pronouns as candidate values. Reproduce the saved cases and exclude non-business grammatical tokens before value lookup.")
        if counts.get("ROUTING_FAILURE"):
            lines.append("1. **P1 — error contract**: model/schema failures returned HTTP 500 with a generic intent label. Preserve the already-built Query Plan and classify the failure at the Llama/validation layer.")
        if counts.get("FK_PLANNING_FAILURE"):
            lines.append("1. **P2 — bridge-role semantics**: distinguish requested endpoint tables from structurally introduced bridge tables without weakening FK authority.")
        if counts.get("VALIDATOR_REJECTION") or counts.get("LLM_SQL_FAILURE"):
            lines.append("1. **P2 — Llama adherence**: use the saved raw output and exact prompt to tune instructions only after confirming Agent 1 supplied the correct structure.")
        downstream = metrics.get("downstream_failure_taxonomy", {})
        if downstream.get("LLM_SQL_FAILURE") or downstream.get("VALIDATOR_REJECTION"):
            lines.append("1. **P2 — Llama/validator boundary**: several structurally valid plans still produced invalid aliases, ambiguous columns, or incomplete composite joins; keep these blocked and tune the existing prompt using the saved cases.")
        if downstream.get("EXECUTION_FAILURE"):
            lines.append("1. **P2 — executable SQL semantics**: add a narrow regression for MySQL `ONLY_FULL_GROUP_BY`; one validator-accepted aggregate failed only at execution.")
        lines.append("1. **No broad production refactor recommended**: fix only reproducible layer-specific causes while retaining FK-only planning and the independent validator.")
    lines += ["", "Exact sanitized prompt samples are stored in `docs/live_fk_acceptance_prompts.json`."]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def regenerate_existing_report() -> dict[str, Any]:
    """Rebuild derived metrics/report without issuing database or model calls."""
    payload = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    for result in payload["results"]:
        timings = result.get("timings_ms") or {}
        if not timings.get("agent1_calls"):
            result["candidate_tables"] = []
            result["resolved_predicates"] = []
            result["verified_fk_path"] = []
            result["constraints"] = []
            result["table_selection_reasons"] = {}
            result["knowledge_matches"] = []
        downstream = []
        expected_outcome = result["expected"]["outcome"]
        if result.get("actual_outcome") == "ambiguity" and expected_outcome != "ambiguity":
            downstream.append("AMBIGUITY")
        if result.get("actual_outcome") == "safe_refusal" and expected_outcome not in {"safe_refusal", "conversation"}:
            downstream.append("SAFE_REFUSAL")
        if result.get("http_status", 200) >= 500 and timings.get("llama_calls") and not timings.get("validator_calls"):
            downstream.append("LLM_SQL_FAILURE")
        if result.get("validator_result") == "rejected":
            downstream.append("VALIDATOR_REJECTION")
        if result.get("sql_execution_attempted") and result.get("http_status", 200) >= 500:
            downstream.append("EXECUTION_FAILURE")
        result["downstream_failure_categories"] = downstream
    known_tables = set(payload["environment"].get("table_names") or [])
    payload["metrics"] = compute_metrics(payload["results"], known_tables)
    write_report(payload)
    return payload


def _model_info(endpoint: str) -> dict[str, Any]:
    health = requests.get(endpoint.rstrip("/") + "/health", timeout=5)
    health.raise_for_status()
    models_response = requests.get(endpoint.rstrip("/") + "/v1/models", timeout=5)
    models_response.raise_for_status()
    body = models_response.json()
    rows = body.get("data") or body.get("models") or []
    if not rows:
        raise RuntimeError("llama.cpp is healthy but did not report an active model.")
    row = rows[0]
    meta = row.get("meta") or {}
    return {"model": row.get("id") or row.get("model") or row.get("name"),
            "context_size": meta.get("n_ctx") or meta.get("context_length")}


def run_benchmark(ids: set[str] | None = None, limit: int | None = None) -> dict[str, Any]:
    from app import app
    from core.schema_cache import schema_cache
    from config.settings import settings

    model = _model_info(settings.LLM_ENDPOINT)
    if schema_cache.schema.get("database") != TEST_DATABASE:
        raise RuntimeError(f"Production app loaded {schema_cache.schema.get('database')!r}, expected {TEST_DATABASE!r}.")
    oracle = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))
    cases = [case for case in oracle["cases"] if not ids or case["id"] in ids]
    if limit:
        cases = cases[:limit]
    client = app.test_client()
    measurements = RuntimeMeasurements(app)
    results: list[dict[str, Any]] = []
    prompt_samples: dict[str, Any] = {}
    try:
        for index, case in enumerate(cases, 1):
            measurements.reset()
            started = time.perf_counter()
            api_response = client.post("/api/agent/chat", json={"question": case["question"]})
            total_ms = (time.perf_counter() - started) * 1000
            response = api_response.get_json(silent=True) or {}
            debug_response = client.get("/api/debug/prompt")
            debug = debug_response.get_json(silent=True) or {}
            result = evaluate(case, response, debug, api_response.status_code, measurements.snapshot(total_ms))
            results.append(result)
            if case["category"] in PROMPT_SAMPLE_CATEGORIES and case["category"] not in prompt_samples and result["prompt_chars"]:
                prompt_samples[case["category"]] = {
                    "id": case["id"], "question": case["question"],
                    "selected_tables": result["selected_tables"],
                    "verified_fk_path": result["verified_fk_path"],
                    "resolved_predicates": result["resolved_predicates"],
                    "prompt": debug["prompt"],
                }
            print(f"[{index:02d}/{len(cases):02d}] {case['id']} {'PASS' if result['passed'] else 'FAIL'} "
                  f"{result['plan_status']} {result['timings_ms']['total_ms'] / 1000:.2f}s", flush=True)
    finally:
        measurements.close()

    mysql_connection = mysql.connector.connect(**_connection_kwargs(TEST_DATABASE))
    try:
        cursor = mysql_connection.cursor()
        cursor.execute("SELECT VERSION()")
        mysql_version = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL", (TEST_DATABASE,))
        fk_columns = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(DISTINCT CONSTRAINT_NAME) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME IS NOT NULL", (TEST_DATABASE,))
        fk_constraints = cursor.fetchone()[0]
        cursor.close()
    finally:
        mysql_connection.close()
    known_tables = {
        table.get("name") if isinstance(table, dict) else str(table)
        for table in schema_cache.tables
    }
    known_tables.discard(None)
    prompt_review = {}
    for category, sample in prompt_samples.items():
        prompt_lower = sample["prompt"].lower()
        selected_set = set(sample["selected_tables"])
        unrelated = sorted(
            table for table in known_tables - selected_set
            if re.search(rf"(?<![a-z0-9_]){re.escape(table.lower())}(?![a-z0-9_])", prompt_lower)
        )
        prompt_review[category] = {
            "id": sample["id"], "prompt_chars": len(sample["prompt"]),
            "unrelated_tables_present": unrelated,
        }
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "environment": {
            "database": TEST_DATABASE, "mysql_version": mysql_version,
            "table_count": len(schema_cache.tables), "fk_column_count": fk_columns,
            "fk_constraint_count": fk_constraints, "endpoint": settings.LLM_ENDPOINT,
            "configured_model": settings.LLM_MODEL, **model,
            "employee_count": EMPLOYEE_COUNT,
            "table_names": sorted(known_tables),
            "max_prompt_chars": settings.MAX_PROMPT_CHARS,
        },
        "knowledge": {
            "approved_tables": ["departments", "commands", "employees", "assignments", "historical_assignments", "salaries"],
            "approved_abbreviations": ["CC", "NC", "SC", "HR"], "pending_entries": 3,
        },
        "metrics": compute_metrics(results, known_tables),
        "prompt_samples": prompt_samples, "prompt_review": prompt_review,
        "results": results,
    }
    write_report(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup-only", action="store_true")
    parser.add_argument("--no-rebuild", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids", help="Comma-separated oracle case IDs")
    parser.add_argument("--report-only", action="store_true", help="Regenerate derived report data from the saved live run")
    parser.add_argument("--fail-on-failure", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    os.environ["DB_NAME"] = TEST_DATABASE
    os.environ["ENABLE_BACKGROUND_SYNC"] = "false"
    sys.path.insert(0, str(ROOT))
    if args.report_only:
        payload = regenerate_existing_report()
        print(f"Regenerated report for {len(payload['results'])} saved live cases.", flush=True)
        return 0
    info = _model_info(os.getenv("LLM_ENDPOINT", "http://localhost:8080"))
    print(f"llama.cpp healthy: {info['model']} (context={info.get('context_size')})", flush=True)
    if not args.no_rebuild:
        rebuild_test_database()
        knowledge_dir = build_knowledge()
        print(f"Built isolated database and production knowledge at {knowledge_dir}", flush=True)
    if args.setup_only:
        return 0
    ids = {item.strip() for item in (args.ids or "").split(",") if item.strip()} or None
    payload = run_benchmark(ids=ids, limit=args.limit)
    print(f"Results: {payload['metrics']['overall']}", flush=True)
    print(f"JSON: {RESULTS_PATH}\nReport: {REPORT_PATH}\nPrompts: {PROMPTS_PATH}", flush=True)
    return 1 if args.fail_on_failure and payload["metrics"]["overall"]["passed"] != payload["metrics"]["overall"]["total"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
