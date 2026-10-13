import pytest
from flask import Flask
from unittest.mock import MagicMock

from app import _direct_response
from agent.agent import database_agent
from core.request_router import RequestRouter, ROUTE_CONVERSATION, ROUTE_DICTIONARY, ROUTE_EXPLANATION, ROUTE_LANGGRAPH, ROUTE_METADATA, ROUTE_RELATIONSHIPS, ROUTE_SCHEMA, ROUTE_TABLE_INFO, ROUTE_UNCLEAR
from routes.chat import bp as chat_blueprint

@pytest.mark.parametrize(("question", "route"), [("show schema", ROUTE_SCHEMA), ("how many tables", ROUTE_METADATA), ("show all tables", ROUTE_METADATA), ("show relationships for employees", ROUTE_RELATIONSHIPS), ("describe employees", ROUTE_TABLE_INFO), ("show business dictionary", ROUTE_DICTIONARY), ("top employees by salary", ROUTE_LANGGRAPH)])
def test_route_patterns(question, route): assert RequestRouter().route(question).route == route
def test_relationship_table_parameter(): assert RequestRouter().route("show relationships for employees").params == {"table":"employees"}
@pytest.mark.parametrize("question", ["explain this sql", "explain the sql query", "what does this sql do"])
def test_sql_explanations_route_before_table_explanations(question): assert RequestRouter().route(question).route == ROUTE_EXPLANATION

@pytest.mark.parametrize("question", ["hello", "hi", "hey", "good morning", "thanks", "thank you", "who are you", "what can you do"])
def test_conversation_never_routes_to_sql(question):
    assert RequestRouter().route(question).route == ROUTE_CONVERSATION

@pytest.mark.parametrize("question", ["show me", "give me data", "tell me something"])
def test_vague_requests_require_clarification(question):
    assert RequestRouter().route(question).route == ROUTE_UNCLEAR

@pytest.mark.parametrize("question", [
    "show employees", "count employees", "list departments", "show employee names",
    "show employees with their departments", "highest salary", "who are the managers",
])
def test_genuine_database_requests_still_route_to_sql(question):
    assert RequestRouter().route(question).route == ROUTE_LANGGRAPH


@pytest.mark.parametrize("endpoint", ["/api/agent/chat", "/api/ask"])
def test_http_conversation_gate_never_calls_database_agent(endpoint, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(chat_blueprint)
    app.extensions["services"] = {
        "router": RequestRouter(), "metadata": object(),
        "direct_response": _direct_response, "last_prompt": {},
    }
    run = MagicMock(side_effect=AssertionError("database agent must not run"))
    monkeypatch.setattr(database_agent, "run", run)

    response = app.test_client().post(endpoint, json={"question": "hello"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["intent"] == ROUTE_CONVERSATION
    assert payload["selected_tables"] == []
    assert payload["database_planning_skipped"] is True
    assert payload["llama_sql_called"] is False
    assert payload["sql_executed"] is False
    run.assert_not_called()
