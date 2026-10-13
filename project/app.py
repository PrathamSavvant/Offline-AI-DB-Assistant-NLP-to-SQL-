"""Flask application factory. HTTP concerns live in routes/, not here."""
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

from agent.agent import database_agent
from agent.database_intelligence_agent import DatabaseIntelligenceAgent
from agent.llama_sql_agent import LlamaSQLAgent
from agent.memory import memory_store
from config.settings import settings
from core.business_dictionary import business_dictionary
from core.context_generator import ContextGenerator
from core.defined_knowledge import DefinedKnowledgeRepository
from core.database import Database, DatabaseConfig
from core.database_registry import database_registry
from core.db_context import db_context
from core.embedding_search import EmbeddingSearch
from core.fuzzy_search import FuzzySearch
from core.join_path_finder import JoinPathFinder
from core.knowledge_search import KnowledgeSearchEngine
from core.llm.llama_cpp_client import LlamaCppClient
from core.pending_dictionary import pending_dictionary
from core.pending_knowledge import PendingKnowledgeRepository
from core.semantic_relationships import SemanticRelationshipRepository
from core.presentation_context import PresentationContextBuilder
from core.query_intent_planner import QueryIntentPlanner
from core.query_plan import QueryPlanBuilder
from core.semantic_context_builder import SemanticContextBuilder
from core.relationship_builder import RelationshipBuilder
from core.request_router import request_router, ROUTE_CONVERSATION, ROUTE_DICTIONARY, ROUTE_METADATA, ROUTE_RELATIONSHIPS, ROUTE_SCHEMA, ROUTE_TABLE_INFO, ROUTE_UNCLEAR
from core.schema_cache import schema_cache
from core.table_annotations import TableAnnotationStore
from core.value_resolver import ValueResolver
from core.predicate_planner import PredicatePlanner
from core.knowledge_feedback import KnowledgeFailureAnalyzer
from routes import register_blueprints
from services.metadata_service import MetadataService
from services.sql_reasoning_engine import PromptBuilder, SQLReasoningEngine, SQLValidator
from services.sql_post_processor import SQLPostProcessor
from services.schema_sync_service import SchemaSyncService
from core.logger import get_logger, flush_logs
from core.serializer import safe_jsonify

logger = get_logger(__name__)


def build_context_generator(annotations=None, cache=schema_cache, dictionary=business_dictionary):
    schema = cache.schema
    defined_knowledge = DefinedKnowledgeRepository(cache.knowledge_dir / "defined_knowledge.json").all()
    return ContextGenerator(
        schema, cache.grouping, cache.semantics, cache.ranking,
        JoinPathFinder(RelationshipBuilder(schema, cache.semantic_relationships)),
        annotations=annotations or {},
        dictionary=dictionary,
        fuzzy_search=FuzzySearch(schema),
        embedding_search=EmbeddingSearch(schema) if settings.ENABLE_EMBEDDINGS else None,
        knowledge_search=KnowledgeSearchEngine(cache.table_profiles),
        table_profiles=cache.table_profiles,
        top_n=settings.MAX_CONTEXT_TABLES,
        inverted_index=cache.inverted_index,
        defined_knowledge=defined_knowledge,
    )


def _direct_response(question, result, metadata):
    route, params = result.route, result.params
    if route == ROUTE_CONVERSATION:
        data = {}
        text = "Hi! Ask me a question about the connected database."
    elif route == ROUTE_UNCLEAR:
        data = {}
        text = "Please tell me which database table, entity, or measure you want to see."
    elif route == ROUTE_METADATA:
        data = metadata.metadata()
        text = (
            f"There are **{data['total_tables']} tables** in database `{data['database_name']}`.\n\n"
            + "\n".join(f"- `{t['name']}` ({t['column_count']} columns)" for t in data["tables"])
        )
    elif route == ROUTE_SCHEMA:
        data = metadata.schema()
        text = "\n".join(
            f"### `{t['name']}`\nColumns: {', '.join(t['columns'])}" for t in data["tables"]
        )
    elif route == ROUTE_TABLE_INFO:
        data = metadata.table(params.get("table", ""))
        text = data.get("error") or (
            f"### Table: `{data['table']}`\n\n"
            + "\n".join(f"- `{c['name']}`" for c in data["columns"])
        )
    elif route == ROUTE_RELATIONSHIPS:
        data = metadata.relationships(params.get("table"))
        rels = data["relationships"]
        text = (
            "\n".join(
                f"- `{r.get('from_table')}.{r.get('from_column')}` → `{r.get('to_table')}.{r.get('to_column')}`"
                for r in rels
            )
            or "No relationships found."
        )
    elif route == ROUTE_DICTIONARY:
        data = metadata.dictionary_entries()
        text = (
            "\n".join(
                f"- **{e['abbreviation']}** = {e.get('expansion', '')}"
                for e in data["entries"]
            )
            or "Business dictionary is empty."
        )
    else:
        raise ValueError(f"Unsupported direct route: {route}")
    skipped = route in {ROUTE_CONVERSATION, ROUTE_UNCLEAR}
    return {
        "question": question,
        "intent": route,
        "final_response": text,
        "selected_tables": [],
        "database_planning_skipped": skipped,
        "llama_sql_called": False,
        "sql_executed": False,
        "query_plan": ({
            "status": "conversation" if route == ROUTE_CONVERSATION else "failed",
            "selected_tables": [],
            "failure_reason": None if route == ROUTE_CONVERSATION else "No sufficiently grounded database entity was provided.",
        } if skipped else None),
        "thinking_steps": [
            {
                "title": "Direct Cache Lookup",
                "detail": "Handled without an LLM.",
                "timestamp": datetime.now().isoformat(),
            }
        ],
        "execution_timeline": [],
        "suggested_followups": [],
    }


def create_app():
    settings.validate()
    schema_cache.load()
    database_registry.load()
    database_registry.touch_connected()
    # A legacy registry may predate the cache it describes. Repair it from
    # fingerprint-verified artifacts without waiting for the 15-minute worker.
    from core.knowledge_readiness import inspect_cached_artifacts, summarize_readiness
    cached_artifacts = inspect_cached_artifacts(schema_cache.knowledge_dir)
    if cached_artifacts["valid"] and cached_artifacts["table_count"] == len(schema_cache.tables):
        database_registry.reconcile_verified_cache(
            cached_artifacts["fingerprint"], cached_artifacts["table_count"],
            cached_artifacts["profile_count"],
        )

    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.SECRET_KEY
    app.config["MAX_CONTENT_LENGTH"] = settings.MAX_CONTENT_LENGTH

    @app.before_request
    def _record_start_time():
        request._start_time = time.perf_counter()
        if request.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            origin = request.headers.get("Origin")
            allowed = {value.strip() for value in settings.CORS_ORIGINS.split(",") if value.strip()}
            if origin and origin != request.host_url.rstrip("/") and "*" not in allowed and origin not in allowed:
                return safe_jsonify(error="Origin is not allowed"), 403
        if request.path in {"/api/agent/chat", "/api/agent/stream", "/api/ask", "/api/execute"}:
            services = app.extensions.get("services")
            if services:
                sync = services.get("schema_sync")
                readiness = summarize_readiness(
                    services["cache"], services.get("registry"), services.get("defined_knowledge"),
                    sync.status() if sync else {}, include_coverage=False,
                )
                if not readiness["schema_ready"]:
                    return safe_jsonify(error="Database knowledge needs synchronization before querying.",
                                        knowledge_readiness=readiness), 503
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return app.make_default_options_response()

    @app.after_request
    def _apply_security_and_cors(response):
        # 1. API request execution duration logging
        if hasattr(request, "_start_time") and request.path.startswith("/api/"):
            duration_ms = (time.perf_counter() - request._start_time) * 1000
            logger.info("%s %s %s %.2fms", request.method, request.path, response.status_code, duration_ms)

        # 2. OWASP Recommended HTTP Security Headers
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data:; "
            "connect-src 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)

        # 3. Dynamic CORS for /api/* routes
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            allowed = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
            if "*" in allowed:
                response.headers["Access-Control-Allow-Origin"] = "*"
            elif origin and origin in allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"

            if "Access-Control-Allow-Origin" in response.headers:
                response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
                response.headers["Access-Control-Max-Age"] = "86400"

        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def _handle_http_error(err):
        code = getattr(err, "code", 500)
        desc = getattr(err, "description", str(err))
        if request.path.startswith("/api/") or not request.accept_mimetypes.accept_html:
            return safe_jsonify(error=desc, status_code=code), code
        return f"<h1>Error {code}</h1><p>{desc}</p>", code

    @app.errorhandler(Exception)
    def _handle_unhandled_exception(exc):
        logger.exception("Unhandled application exception on %s %s: %s", request.method, request.path, exc)
        if request.path.startswith("/api/") or not request.accept_mimetypes.accept_html:
            return safe_jsonify(error="An internal server error occurred.", status_code=500), 500
        return "<h1>500 Internal Server Error</h1><p>An unexpected error occurred.</p>", 500

    annotations = TableAnnotationStore()
    defined_knowledge = DefinedKnowledgeRepository()
    pending_knowledge = PendingKnowledgeRepository()
    semantic_relationships = SemanticRelationshipRepository()
    defined_knowledge.migrate(schema_cache.schema, annotations.get_all(), business_dictionary)
    pending_knowledge.migrate(pending_dictionary.get_all())
    context = build_context_generator(annotations.get_all())
    presentation = PresentationContextBuilder(
        schema_cache.schema, schema_cache.semantics, annotations.get_all(),
        business_dictionary, schema_cache.relationships,
        schema_cache.table_profiles,
    )
    value_resolver = ValueResolver(
        schema_cache.schema, business_dictionary, schema_cache.semantics,
        annotations.get_all(), database_adapter=Database, defined_knowledge=defined_knowledge,
    )
    predicate_planner = PredicatePlanner(schema_cache.schema, business_dictionary, schema_cache.semantics, value_resolver)
    feedback_analyzer = KnowledgeFailureAnalyzer(pending_knowledge)
    query_intent = QueryIntentPlanner(schema_cache.schema, schema_cache.semantics, schema_cache.relationships, business_dictionary, schema_cache.table_profiles, defined_knowledge)
    semantic = SemanticContextBuilder(
        schema_cache.schema, schema_cache.semantics, annotations.get_all(), schema_cache.relationships, schema_cache.table_profiles, defined_knowledge,
    )
    post_processor = SQLPostProcessor(schema_cache.schema, business_dictionary)
    llm = LlamaCppClient(
        settings.LLM_MODEL,
        settings.LLM_ENDPOINT,
        settings.LLM_TIMEOUT,
        settings.LLM_TEMPERATURE,
        settings.LLM_MAX_TOKENS,
        settings.LLM_REPEAT_PENALTY,
    )
    if os.environ.get('OFFLINE_LAUNCHER') != '1':
        try:
            llm.health_check()
        except Exception as exc:
            logger.warning("LLM server health check failed (continuing in offline/fallback mode): %s", exc)
    last_prompt = {"question": None, "selected_tables": [], "prompt": None, "timestamp": None}
    intelligence_agent = DatabaseIntelligenceAgent(
            context, presentation, semantic_builder=semantic, intent_planner=query_intent,
            value_resolver=value_resolver, predicate_planner=predicate_planner,
            failure_analyzer=feedback_analyzer,
            query_plan_builder=QueryPlanBuilder(schema_cache.schema),
    )
    prompt_builder = PromptBuilder(business_dictionary)
    llama_agent = LlamaSQLAgent(prompt_builder, llm)
    engine = SQLReasoningEngine(
        intelligence_agent, prompt_builder, SQLValidator(), Database, llm,
        last_prompt, post_processor, llama_agent, request_router,
    )
    database_agent.configure(engine)
    app.extensions["knowledge_impact_builder"] = engine.context_builder

    services = {
        "router": request_router,
        "cache": schema_cache,
        "registry": database_registry,
        "context": context,
        "presentation": presentation,
        "query_intent": query_intent,
        "database_intelligence_agent": intelligence_agent,
        "llama_sql_agent": llama_agent,
        "semantic": semantic,
        "knowledge_search": KnowledgeSearchEngine(schema_cache.table_profiles),
        "post_processor": post_processor,
        "annotations": annotations,
        "defined_knowledge": defined_knowledge,
        "pending_knowledge": pending_knowledge,
        "semantic_relationships": semantic_relationships,
        "dictionary": business_dictionary,
        "pending": pending_dictionary,
        "metadata": MetadataService(schema_cache, business_dictionary),
        "validator": SQLValidator(),
        "database_factory": Database,
        "llm": llm,
        "last_prompt": last_prompt,
        "direct_response": _direct_response,
        "database_switch_lock": threading.RLock(),
    }

    def refresh_runtime_context() -> None:
        """
        Full runtime refresh after a database switch or schema rebuild.

        Execution order:
          1. Reload per-database knowledge files from knowledge/<DB_NAME>/
          2. Reload user-owned stores (dictionary, annotations, pending, registry)
          3. Rebuild context generator, fuzzy search, embedding search
          4. Update all service references
          5. Clear agent thread memory to prevent stale cross-database context
          6. Reset prompt debugger log
        """
        # 1. Knowledge files & Registry
        schema_cache.load()
        database_registry.load()
        database_registry.touch_connected()

        # 2. User-owned stores — reload from knowledge/<active_db>/
        business_dictionary.reload()
        annotations.reload()
        pending_dictionary.reload()
        defined_knowledge.reload()
        pending_knowledge.reload()
        semantic_relationships.reload()
        defined_knowledge.migrate(schema_cache.schema, annotations.get_all(), business_dictionary)
        pending_knowledge.migrate(pending_dictionary.get_all())

        # 3. Context generator and search indices
        refreshed = build_context_generator(annotations.get_all())
        refreshed_presentation = PresentationContextBuilder(
            schema_cache.schema, schema_cache.semantics, annotations.get_all(),
            business_dictionary, schema_cache.relationships,
            schema_cache.table_profiles,
        )
        refreshed_value_resolver = ValueResolver(
            schema_cache.schema, business_dictionary, schema_cache.semantics,
            annotations.get_all(), database_adapter=Database, defined_knowledge=defined_knowledge,
        )
        refreshed_predicate_planner = PredicatePlanner(schema_cache.schema, business_dictionary, schema_cache.semantics, refreshed_value_resolver)
        refreshed_feedback_analyzer = KnowledgeFailureAnalyzer(pending_knowledge)
        refreshed_query_intent = QueryIntentPlanner(schema_cache.schema, schema_cache.semantics, schema_cache.relationships, business_dictionary, schema_cache.table_profiles, defined_knowledge)
        refreshed_semantic = SemanticContextBuilder(
            schema_cache.schema, schema_cache.semantics, annotations.get_all(), schema_cache.relationships, schema_cache.table_profiles, defined_knowledge,
        )
        refreshed_post_processor = SQLPostProcessor(schema_cache.schema, business_dictionary)
        # Construct a replacement engine with an immutable connection target.
        # This avoids mutating an engine while an earlier request is using it.
        new_prompt = {"question": None, "selected_tables": [], "prompt": None, "timestamp": None}
        config = DatabaseConfig.from_settings()
        refreshed_intelligence_agent = DatabaseIntelligenceAgent(
                refreshed, refreshed_presentation, semantic_builder=refreshed_semantic, intent_planner=refreshed_query_intent,
                value_resolver=refreshed_value_resolver, predicate_planner=refreshed_predicate_planner,
                failure_analyzer=refreshed_feedback_analyzer,
                query_plan_builder=QueryPlanBuilder(schema_cache.schema),
        )
        refreshed_prompt_builder = PromptBuilder(business_dictionary)
        refreshed_llama_agent = LlamaSQLAgent(refreshed_prompt_builder, llm)
        replacement = SQLReasoningEngine(
            refreshed_intelligence_agent, refreshed_prompt_builder, SQLValidator(),
            lambda: Database(config), llm, new_prompt, refreshed_post_processor,
            refreshed_llama_agent, request_router,
        )
        with services["database_switch_lock"]:
            database_agent.configure(replacement)
            app.extensions["knowledge_impact_builder"] = replacement.context_builder
            services.update({
                "context": refreshed,
                "presentation": refreshed_presentation,
                "query_intent": refreshed_query_intent,
                "database_intelligence_agent": refreshed_intelligence_agent,
                "llama_sql_agent": refreshed_llama_agent,
                "semantic": refreshed_semantic,
                "knowledge_search": KnowledgeSearchEngine(schema_cache.table_profiles),
                "post_processor": refreshed_post_processor,
                "metadata": MetadataService(schema_cache, business_dictionary),
                "registry": database_registry,
                "database_factory": lambda: Database(config),
                "last_prompt": new_prompt,
                "runtime_database": config.database,
            })

        # 5. Purge stale agent thread memory
        memory_store.clear_all()

        # 6. Reset prompt debug log
        last_prompt.update(
            {"question": None, "selected_tables": [], "prompt": None, "timestamp": None}
        )

    services["refresh_runtime_context"] = refresh_runtime_context

    project_dir = Path(__file__).resolve().parent

    def rebuild_knowledge(staging_dir: Path) -> None:
        # Pass the *current* runtime database context as environment overrides so
        # the subprocess targets the correct database even after a live DB switch.
        env = os.environ.copy()
        env.update(
            {
                "DB_HOST": settings.DB_HOST,
                "DB_PORT": str(settings.DB_PORT),
                "DB_USER": settings.DB_USER,
                "DB_PASSWORD": settings.DB_PASSWORD,
                "DB_NAME": db_context.active_database,
                "KNOWLEDGE_OUTPUT_DIR": str(staging_dir),
            }
        )
        subprocess.run(
            [sys.executable, str(project_dir / "main.py"), "--force"],
            cwd=project_dir,
            check=True,
            env=env,
        )

    # SchemaSyncService now resolves its knowledge directory dynamically via
    # db_context.get_knowledge_dir() — no path is passed in the constructor.
    sync_service = SchemaSyncService(schema_cache, refresh_runtime_context, rebuild_knowledge)
    services["schema_sync"] = sync_service
    app.extensions["services"] = services
    register_blueprints(app)

    if settings.ENABLE_BACKGROUND_SYNC:
        sync_service.start_background_worker(settings.SYNC_INTERVAL_MINUTES * 60)

    def shutdown():
        """Graceful application shutdown hook."""
        logger.info("Executing graceful application shutdown...")
        if "schema_sync" in services:
            services["schema_sync"].stop_background_worker(timeout=5.0)
        Database.clear_pools()
        flush_logs()
        logger.info("Application shutdown complete.")

    app.shutdown = shutdown
    services["shutdown"] = shutdown

    return app


app = create_app()
if __name__ == "__main__":
    # use_reloader=False: the werkzeug file-change watchdog detects edits to
    # threading.py (via the schema-sync background thread) and restarts the
    # entire process, which then fails to start if knowledge files are
    # temporarily missing. The background sync thread is already fully
    # exception-safe; we do not need the watchdog's crash-recovery here.
    app.run(debug=True, use_reloader=False)
