from collections import deque
from pathlib import Path
import threading
import time

import requests
from flask import Blueprint, current_app, render_template, request, redirect

from config.settings import settings
from core.database import Database
from core.db_context import db_context
from core.knowledge_readiness import summarize_readiness
from core.serializer import safe_jsonify

from datetime import datetime, timezone

bp = Blueprint("meta", __name__)
PAGES = {"/": "index.html", "/defined-knowledge": "defined_knowledge.html", "/pending-knowledge": "pending_knowledge.html", "/debug": "debug.html", "/crawler-dashboard": "crawler_dashboard.html", "/schema-changes": "schema_changes.html"}
SERVER_START_TIME = datetime.now(timezone.utc)
_LLM_HEALTH = {"endpoint": None, "checked_at": 0.0, "reachable": False}
_LLM_HEALTH_LOCK = threading.Lock()
PAGES['/schema-map'] = 'schema_map.html'


def s(): return current_app.extensions["services"]


def _llm_reachable() -> bool:
    """Avoid probing a CPU-bound local model on every UI health poll."""
    with _LLM_HEALTH_LOCK:
        now = time.monotonic()
        endpoint = settings.LLM_ENDPOINT
        if _LLM_HEALTH["endpoint"] == endpoint and now - _LLM_HEALTH["checked_at"] < 10:
            return _LLM_HEALTH["reachable"]
        try:
            reachable = requests.get(f"{endpoint}/health", timeout=3).ok
        except Exception:
            reachable = False
        _LLM_HEALTH.update(endpoint=endpoint, checked_at=now, reachable=reachable)
        return reachable


for url, template in PAGES.items():
    bp.add_url_rule(url, endpoint="page_" + template.replace(".", "_"), view_func=lambda template=template: render_template(template))


@bp.get("/defined-knowledge/<table_name>")
def table_knowledge_page(table_name):
    return render_template("table_knowledge.html", table_name=table_name)


@bp.get("/pending-knowledge/<table_name>")
def pending_table_review_page(table_name):
    return render_template("pending_table_review.html", table_name=table_name)


# Retire old management pages without breaking bookmarked URLs.
@bp.get("/tables")
@bp.get("/dictionary")
def legacy_defined_knowledge_page():
    return redirect("/defined-knowledge", code=302)


@bp.get("/pending-dictionary")
def legacy_pending_knowledge_page():
    return redirect("/pending-knowledge", code=302)


@bp.get("/healthz")
def liveness():
    """Lightweight Kubernetes / container liveness probe."""
    uptime = int((datetime.now(timezone.utc) - SERVER_START_TIME).total_seconds())
    return safe_jsonify(status="alive", uptime_seconds=uptime)


@bp.get("/readyz")
def readiness():
    """Kubernetes / container readiness probe verifying schema and database health."""
    cache = s().get("cache")
    if not cache or not cache.is_loaded():
        return safe_jsonify(status="not_ready", reason="Schema cache is not loaded"), 503
    knowledge = summarize_readiness(cache, s().get("registry"), s().get("defined_knowledge"),
                                    s().get("schema_sync").status() if s().get("schema_sync") else {})
    if not knowledge["schema_ready"]:
        return safe_jsonify(status="not_ready", reason="Schema knowledge needs attention",
                            knowledge_readiness=knowledge), 503
    try:
        db = Database()
        conn = db.connect()
        conn.close()
    except Exception as exc:
        return safe_jsonify(status="not_ready", reason=f"Database unreachable: {exc}"), 503
    return safe_jsonify(status="ready", database=db_context.active_database)


@bp.get("/api/health")
def health():
    ok = _llm_reachable()
    reg = s().get("registry")
    reg_data = reg.get() if reg else {}
    sync_service = s().get("schema_sync")
    sync_status = sync_service.status() if sync_service else {}
    readiness = summarize_readiness(s()["cache"], reg, s().get("defined_knowledge"), sync_status)
    uptime = int((datetime.now(timezone.utc) - SERVER_START_TIME).total_seconds())
    return safe_jsonify(
        status="ok" if readiness["schema_ready"] else "attention",
        uptime_seconds=uptime,
        environment=settings.ENVIRONMENT,
        database=db_context.active_database,
        database_id=reg_data.get("database_id"),
        knowledge_version=reg_data.get("knowledge_version"),
        knowledge_status=reg_data.get("knowledge_status", "ready"),
        last_scan=reg_data.get("last_scan"),
        llm_model=settings.LLM_MODEL,
        llm_reachable=ok,
        tables_loaded=len(s()["cache"].tables),
        schema_cached=s()["cache"].is_loaded(),
        pool_status=Database.get_pool_status(),
        sync_status=sync_status.get("status", "unknown"),
        last_sync_at=sync_status.get("last_sync_at"),
        knowledge_readiness=readiness,
    )


@bp.get("/api/database/registry")
def database_registry_info():
    reg = s().get("registry")
    if not reg:
        return safe_jsonify(error="Database registry not available"), 500
    data = reg.get()
    # Calculate needs_review_total across all table profiles
    profiles = s()["cache"].table_profiles
    needs_review_total = 0
    for profile in profiles.values():
        manual_abbrs = profile.get("manual_knowledge", {}).get("abbreviations", {})
        needs_review_total += sum(
            1 for entry in manual_abbrs.values()
            if isinstance(entry, dict) and entry.get("status") == "needs_review"
        )
    return safe_jsonify(**data, needs_review_total=needs_review_total,
                        knowledge_readiness=summarize_readiness(
                            s()["cache"], reg, s().get("defined_knowledge"),
                            s().get("schema_sync").status() if s().get("schema_sync") else {},
                        ))


@bp.get("/api/debug/prompt")
def prompt():
    if not settings.ENABLE_SENSITIVE_DEBUG:
        return safe_jsonify(error="Sensitive prompt diagnostics are disabled."), 403
    return safe_jsonify(s()["last_prompt"])


@bp.get("/api/logs")
def logs():
    if not settings.ENABLE_SENSITIVE_DEBUG:
        return safe_jsonify(error="Sensitive log diagnostics are disabled."), 403
    path = Path(current_app.root_path) / "logs" / "engine.log"
    raw_count = request.args.get("lines", 200, type=int)
    count = max(1, min(raw_count if raw_count is not None else 200, 2000))
    return safe_jsonify(lines=[line.rstrip() for line in deque(path.open(encoding="utf-8", errors="replace"), maxlen=count)] if path.exists() else [])


@bp.get("/api/llm/models")
def models():
    try:
        response = requests.get(f"{settings.LLM_ENDPOINT}/v1/models", timeout=5)
        response.raise_for_status()
        models = [{"name": model["id"], "label": model["id"].split(":")[0].title()} for model in response.json().get("data", [])]
        return safe_jsonify(models=models, active=settings.LLM_MODEL)
    except Exception as exc: return safe_jsonify(models=[], active=settings.LLM_MODEL, error=str(exc))


@bp.post("/api/llm/model")
def model():
    value = ((request.get_json(silent=True) or {}).get("model") or "").strip()
    if not value: return safe_jsonify(error="'model' is required"), 400
    settings.LLM_MODEL = value
    s()["llm"].model = value
    return safe_jsonify(active=value, status="switched")


@bp.post("/api/connection/test")
def connection_test():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or settings.DB_HOST).strip()
    port = int(data.get("port") or settings.DB_PORT)
    user = (data.get("user") or settings.DB_USER).strip()
    password = data.get("password") if data.get("password") is not None else settings.DB_PASSWORD
    dbname = (data.get("database") or settings.DB_NAME).strip()
    try:
        import mysql.connector
        conn = mysql.connector.connect(host=host, port=port, user=user, password=password, database=dbname, connect_timeout=5)
        conn.close()
        return safe_jsonify(status="ok", message=f"Successfully connected to database '{dbname}' at {host}:{port}")
    except Exception as exc: return safe_jsonify(status="error", error=str(exc)), 400


@bp.post("/api/connection/connect")
def connection_connect():
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or settings.DB_HOST).strip()
    port = int(data.get("port") or settings.DB_PORT)
    user = (data.get("user") or settings.DB_USER).strip()
    password = data.get("password") if data.get("password") is not None else settings.DB_PASSWORD
    dbname = (data.get("database") or settings.DB_NAME).strip()
    if not dbname or not user or not host: return safe_jsonify(error="host, user, and database are required"), 400
    try:
        import mysql.connector
        conn = mysql.connector.connect(host=host, port=port, user=user, password=password, database=dbname, connect_timeout=5)
        conn.close()

        services = s()
        with services["database_switch_lock"]:
            previous = {"database": db_context.active_database, "host": settings.DB_HOST, "port": settings.DB_PORT, "user": settings.DB_USER, "password": settings.DB_PASSWORD}
            try:
                db_context.set_active_database(dbname, host=host, port=port, user=user, password=password)
                Database._pool = None
                sync_service = services.get("schema_sync")
                result = sync_service.synchronize(trigger="connection_change") if sync_service else {}
                if result.get("status") in {"failed", "already_running"}:
                    raise RuntimeError("Database switch synchronization is not available")
                if result.get("status") == "unchanged":
                    services["refresh_runtime_context"]()
                _write_env_file(host, port, user, password, dbname)
            except Exception:
                db_context.set_active_database(previous["database"], host=previous["host"], port=previous["port"], user=previous["user"], password=previous["password"])
                Database._pool = None
                services["refresh_runtime_context"]()
                raise
        return safe_jsonify(status="connected", database=db_context.active_database, host=host, port=port, sync=result)
    except Exception as exc: return safe_jsonify(status="error", error=str(exc)), 400


def _write_env_file(host: str, port: int, user: str, password: str, dbname: str) -> None:
    """Persist the completed, coherent database switch atomically."""
    env_path = Path(current_app.root_path) / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        updates = {"DB_HOST": host, "DB_PORT": str(port), "DB_USER": user, "DB_PASSWORD": password, "DB_NAME": dbname}
        written, new_lines = set(), []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                new_lines.append(line); continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}"); written.add(key)
            else: new_lines.append(line)
        new_lines.extend(f"{key}={value}" for key, value in updates.items() if key not in written)
        temporary = env_path.with_suffix(".tmp")
        temporary.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        temporary.replace(env_path)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Could not persist DB settings to .env: %s", exc)
