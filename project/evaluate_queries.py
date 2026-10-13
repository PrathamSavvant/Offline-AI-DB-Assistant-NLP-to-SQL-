"""Run approved golden questions through the deterministic agent, without Llama.

Usage: python evaluate_queries.py --cases path/to/private_cases.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True, help="Private JSON suite for the active database")
    parser.add_argument("--output", type=Path, help="Optional local JSON report (no questions or values)")
    parser.add_argument("--full-pipeline", action="store_true",
                        help="Also call local Llama and execute read-only validated SQL on this database")
    parser.add_argument("--cached-only", action="store_true",
                        help="Skip live schema verification (deterministic diagnostics only)")
    args = parser.parse_args()
    suite = json.loads(args.cases.read_text(encoding="utf-8"))
    if not isinstance(suite, dict) or not isinstance(suite.get("cases"), list):
        parser.error("Suite must be an object containing a cases list")
    ids = [case.get("id") for case in suite["cases"] if isinstance(case, dict)]
    if len(ids) != len(suite["cases"]) or not all(isinstance(item, str) and item for item in ids) or len(ids) != len(set(ids)):
        parser.error("Every case needs a unique non-empty string id")
    if not all(isinstance(case.get("question"), str) and case["question"].strip() for case in suite["cases"]):
        parser.error("Every case needs a non-empty question")

    # Importing the application wires its real deterministic pipeline. Avoid
    # starting the background worker or probing the local model in this CLI.
    os.environ["ENABLE_BACKGROUND_SYNC"] = "false"
    os.environ["OFFLINE_LAUNCHER"] = "1"
    from app import app
    from core.db_context import db_context
    from core.query_evaluation import run_suite, evaluate_full_case
    from core import schema_fingerprint
    services = app.extensions["services"]
    if args.cached_only and args.full_pipeline:
        parser.error("--full-pipeline requires a live schema check")
    if not args.cached_only:
        sync = services["schema_sync"].synchronize(trigger="acceptance")
        if sync.get("status") in {"failed", "already_running"}:
            print("Live schema verification failed; no queries were tested.", file=sys.stderr)
            return 2
    database_id = suite.get("database_id")
    fingerprint = suite.get("schema_fingerprint")
    if database_id != db_context.database_id or fingerprint != schema_fingerprint.compute(services["cache"].schema):
        print("Suite database identity or schema fingerprint does not match the active cache.", file=sys.stderr)
        return 2
    report = run_suite(services["database_intelligence_agent"], suite["cases"])
    report["database_id"] = database_id
    report["schema_fingerprint"] = fingerprint
    if args.full_pipeline:
        from agent.agent import database_agent
        report["full_pipeline"] = [evaluate_full_case(database_agent._engine(), case) for case in suite["cases"]]
        report["full_pipeline_passed"] = sum(item["passed"] for item in report["full_pipeline"])
    for item in report["cases"]:
        print(f"{item['id']}: {'PASS' if item['passed'] else 'FAIL'} ({item['elapsed_ms']} ms)"
              + (f" — {', '.join(item['failures'])}" if item["failures"] else ""))
    print(f"Passed {report['passed']}/{report['total']} | mean {report['mean_ms']} ms")
    if args.full_pipeline:
        print(f"Full pipeline passed {report['full_pipeline_passed']}/{report['total']}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if report["failed"] or (args.full_pipeline and report["full_pipeline_passed"] != report["total"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
