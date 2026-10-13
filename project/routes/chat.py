import json
from flask import Blueprint, Response, current_app, request
from agent.agent import database_agent
from core.request_router import ROUTE_EXPLANATION, ROUTE_LANGGRAPH
from core.serializer import safe_dumps, safe_jsonify
from core.logger import get_logger
from services.sql_reasoning_engine import DatabaseExecutor

bp = Blueprint("chat", __name__)
logger = get_logger(__name__)
def _services(): return current_app.extensions["services"]
def _question(): return ((request.get_json(silent=True) or {}).get("question") or request.args.get("question") or "").strip()

def _direct(question, result):
    services = _services()
    response = services["direct_response"](question, result, services["metadata"])
    if response.get("database_planning_skipped"):
        plan = response.get("query_plan") or {}
        services["last_prompt"].update({
            "question": question,
            "intent": result.route.lower(),
            "database_planning_skipped": True,
            "selected_tables": [],
            "plan_status": plan.get("status"),
            "failure_reason": plan.get("failure_reason"),
            "llama_sql_called": False,
            "sql_executed": False,
            "prompt": None,
            "sql": None,
        })
    return response

@bp.post("/api/agent/chat")
def chat():
    question = _question(); body = request.get_json(silent=True) or {}
    if not question: return safe_jsonify(error="Please provide a question."), 400
    router, metadata = _services()["router"], _services()["metadata"]
    result = router.route(question)
    if result.route == ROUTE_EXPLANATION:
        try: return safe_jsonify(database_agent.explain_sql((body.get("thread_id") or "").strip() or None))
        except ValueError as exc: return safe_jsonify(error=str(exc)), 400
        except Exception as exc: return safe_jsonify(error=f"SQL explanation failed: {exc}"), 502
    if result.route != ROUTE_LANGGRAPH: return safe_jsonify(_direct(question, result))
    try: return safe_jsonify(database_agent.run(question, (body.get("thread_id") or "").strip()))
    except Exception as exc:
        logger.exception("AI chat request failed")
        return safe_jsonify(error=f"Agent error: {exc}"), 500

@bp.route("/api/agent/stream", methods=["POST", "GET"])
def stream():
    question = _question(); body = request.get_json(silent=True) or {}
    if not question: return safe_jsonify(error="Please provide a question."), 400
    result = _services()["router"].route(question)
    def events():
        if result.route == ROUTE_EXPLANATION:
            try:
                payload = database_agent.explain_sql((body.get("thread_id") or "").strip() or None)
                yield "event: message\ndata: " + safe_dumps(payload) + "\n\n"
            except Exception as exc:
                yield "event: error\ndata: " + safe_dumps({"error": str(exc)}) + "\n\n"
        elif result.route != ROUTE_LANGGRAPH:
            yield "event: message\ndata: " + safe_dumps(_direct(question, result)) + "\n\n"
        else:
            for event in database_agent.stream(question, (body.get("thread_id") or "").strip()): yield f"event: {event['event']}\ndata: {safe_dumps(event['data'])}\n\n"
        yield "event: done\ndata: {\"status\": \"complete\"}\n\n"
    return Response(events(), mimetype="text/event-stream")

@bp.post("/api/ask")
def ask():
    question = _question()
    if not question: return safe_jsonify(error="Please provide a question."), 400
    result = _services()["router"].route(question)
    if result.route != ROUTE_LANGGRAPH:
        return safe_jsonify(_direct(question, result))
    try: return safe_jsonify(database_agent.run(question))
    except Exception as exc: return safe_jsonify(error=str(exc)), 500

@bp.post("/api/execute")
def execute():
    sql = ((request.get_json(silent=True) or {}).get("sql") or "").strip()
    if not sql: return safe_jsonify(error="No SQL provided."), 400
    try:
        validated = _services()["validator"].validate(sql, {"selected_tables": [t["name"] for t in _services()["cache"].tables]})
        return safe_jsonify(DatabaseExecutor(_services()["database_factory"]()).execute(validated))
    except Exception as exc: return safe_jsonify(sql=sql, error=str(exc)), 400
