"""
Offline & Production deployment preflight.
Validates dependencies, configuration, filesystem permissions, database connectivity,
knowledge-base integrity, and LLM reachability before starting the production server.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Tuple

from config.settings import settings


REQUIRED_PACKAGES = [
    ("flask", "Flask web framework"),
    ("mysql.connector", "MySQL driver connector"),
    ("sqlglot", "SQL parser and AST validator"),
    ("waitress", "Production WSGI server"),
    ("requests", "HTTP client"),
    ("rapidfuzz", "Fuzzy search engine"),
]


def check_python_version() -> Tuple[bool, str]:
    v = sys.version_info
    if v.major == 3 and v.minor >= 10:
        return True, f"Python {v.major}.{v.minor}.{v.micro}"
    return False, f"Python {v.major}.{v.minor}.{v.micro} detected. Python 3.10+ is recommended."


def check_packages() -> Tuple[List[str], List[str]]:
    passed, missing = [], []
    for pkg, desc in REQUIRED_PACKAGES:
        if importlib.util.find_spec(pkg) is not None:
            passed.append(f"{pkg} ({desc})")
        else:
            missing.append(f"{pkg} ({desc})")
    return passed, missing


def check_settings() -> Tuple[bool, str]:
    try:
        settings.validate()
        return True, f"Settings valid for database '{settings.DB_NAME}' at {settings.DB_HOST}:{settings.DB_PORT}"
    except ValueError as exc:
        return False, str(exc)


def check_filesystem() -> Tuple[List[str], List[str]]:
    passed, errors = [], []
    project_dir = Path(__file__).resolve().parent

    for d in ("logs", "knowledge"):
        path = project_dir / d
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".perm_check.tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            passed.append(f"Writable directory: {d}/")
        except Exception as exc:
            errors.append(f"Cannot write to directory '{d}/': {exc}")

    return passed, errors


def check_knowledge_base() -> Tuple[bool, str]:
    from core.db_context import db_context
    k_dir = db_context.get_knowledge_dir()
    required = ["schema.json", "table_groups.json", "business_semantics.json", "table_importance.json"]
    missing = [f for f in required if not (k_dir / f).exists()]
    if missing:
        return False, f"Missing knowledge files in {k_dir.name}/: {', '.join(missing)}. Run `python main.py --force` to build."
    return True, f"Knowledge base verified for '{db_context.active_database}' ({len(required)} core artifacts present)"


def check_database() -> Tuple[bool, str]:
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            connect_timeout=min(settings.DB_CONNECT_TIMEOUT, 3),
        )
        conn.close()
        return True, f"MySQL connection successful to {settings.DB_USER}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    except Exception as exc:
        return False, f"MySQL connection failed: {exc}"


def check_llm() -> Tuple[bool, str]:
    try:
        import requests
        resp = requests.get(f"{settings.LLM_ENDPOINT}/health", timeout=3)
        if resp.ok:
            return True, f"LLM server reachable at {settings.LLM_ENDPOINT}"
        return False, f"LLM server returned HTTP {resp.status_code} at {settings.LLM_ENDPOINT}/health"
    except Exception as exc:
        return False, f"LLM server unreachable at {settings.LLM_ENDPOINT} ({exc})"


def run_preflight(check_db: bool = True, check_llm_server: bool = True) -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "passed": True,
        "checks": {},
        "errors": [],
        "warnings": [],
    }

    # Python version
    py_ok, py_msg = check_python_version()
    results["checks"]["python"] = {"ok": py_ok, "message": py_msg}
    if not py_ok:
        results["warnings"].append(py_msg)

    # Dependencies
    pkg_passed, pkg_missing = check_packages()
    results["checks"]["dependencies"] = {"passed": pkg_passed, "missing": pkg_missing}
    if pkg_missing:
        results["errors"].append(f"Missing required packages: {', '.join(pkg_missing)}")

    # Settings
    cfg_ok, cfg_msg = check_settings()
    results["checks"]["configuration"] = {"ok": cfg_ok, "message": cfg_msg}
    if not cfg_ok:
        results["errors"].append(cfg_msg)

    # Filesystem
    fs_passed, fs_errors = check_filesystem()
    results["checks"]["filesystem"] = {"passed": fs_passed, "errors": fs_errors}
    if fs_errors:
        results["errors"].extend(fs_errors)

    # Knowledge
    kb_ok, kb_msg = check_knowledge_base()
    results["checks"]["knowledge_base"] = {"ok": kb_ok, "message": kb_msg}
    if not kb_ok:
        results["warnings"].append(kb_msg)

    # Live Database Check
    if check_db and cfg_ok:
        db_ok, db_msg = check_database()
        results["checks"]["database"] = {"ok": db_ok, "message": db_msg}
        if not db_ok:
            results["errors"].append(db_msg)

    # Live LLM Check
    if check_llm_server:
        llm_ok, llm_msg = check_llm()
        results["checks"]["llm"] = {"ok": llm_ok, "message": llm_msg}
        if not llm_ok:
            results["warnings"].append(llm_msg)

    results["passed"] = len(results["errors"]) == 0
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Production & Offline Deployment Preflight Verification")
    parser.add_argument("--strict", action="store_true", help="Fail if any warning is encountered")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")
    parser.add_argument("--skip-db", action="store_true", help="Skip live MySQL connection verification")
    parser.add_argument("--skip-llm", action="store_true", help="Skip live LLM endpoint verification")
    parser.add_argument("--quiet", action="store_true", help="Suppress non-error output")
    args = parser.parse_args()

    results = run_preflight(check_db=not args.skip_db, check_llm_server=not args.skip_llm)

    if args.strict and results["warnings"]:
        results["passed"] = False

    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if results["passed"] else 1

    if not args.quiet:
        print("================================================================")
        print("           AI Database Assistant - Preflight Verification       ")
        print("================================================================")
        print(f"[*] Environment: {settings.ENVIRONMENT}")
        print(f"[*] Python:      {sys.version.split()[0]}")
        print(f"[*] Target DB:   {settings.DB_USER}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
        print(f"[*] LLM Target:  {settings.LLM_ENDPOINT} (model: {settings.LLM_MODEL})")
        print("----------------------------------------------------------------")

        for key, details in results["checks"].items():
            if isinstance(details, dict):
                status_symbol = "[OK]" if details.get("ok", True) and not details.get("missing") and not details.get("errors") else "[WARN/FAIL]"
                message = details.get("message") or details.get("passed") or ""
                if isinstance(message, list):
                    message = f"{len(message)} checked"
                print(f"{status_symbol:<12} {key.replace('_', ' ').title()}: {message}")

        if results["warnings"]:
            print("\nWARNINGS:")
            for w in results["warnings"]:
                print(f"  [!] {w}")

        if results["errors"]:
            print("\nERRORS:")
            for e in results["errors"]:
                print(f"  [X] {e}")

        print("================================================================")
        if results["passed"]:
            print("[SUCCESS] Preflight checks passed. Application is ready to run.")
        else:
            print("[FAILED] Preflight checks failed. Please fix the errors above.")
        print("================================================================")

    return 0 if results["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
