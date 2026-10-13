from unittest.mock import MagicMock, patch

from core.llm.llama_cpp_client import LlamaCppClient


def _response(content: str):
    response = MagicMock()
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


def test_health_check_uses_llama_cpp_health_endpoint():
    client = LlamaCppClient(model="local-model", host="http://localhost:8080")
    response = MagicMock()
    with patch("requests.get", return_value=response) as get:
        client.health_check()
    get.assert_called_once_with("http://localhost:8080/health", timeout=5)


def test_structured_sql_preserves_existing_decision_contract():
    client = LlamaCppClient(model="local-model", host="http://localhost:8080")
    content = '{"sql":"SELECT 1;","answer":"One row.","confidence":1,"needs_clarification":false}'
    with patch("requests.post", return_value=_response(content)) as post:
        decision = client.generate_structured_sql("schema prompt")
    assert decision["sql"] == "SELECT 1;"
    assert decision["answer"] == "One row."
    assert post.call_args.args[0] == "http://localhost:8080/v1/chat/completions"
    response_format = post.call_args.kwargs["json"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == client.SQL_DECISION_SCHEMA
    assert post.call_args.kwargs["json"]["max_tokens"] == 384
    assert post.call_args.kwargs["json"]["repeat_penalty"] == 1.15


def test_structured_sql_normalizes_fenced_reasoning_output():
    client = LlamaCppClient(model="local-model", host="http://localhost:8080")
    content = (
        "<think>Determine the safe query first.</think>\n"
        "Here is the decision:\n"
        "```json\n"
        '{"query":"SELECT 1;","message":"One row.",'
        '"confidence_score":1,"needsClarification":false}'
        "\n```\n"
        "Done."
    )
    with patch("requests.post", return_value=_response(content)):
        decision = client.generate_structured_sql("schema prompt")
    assert decision == {
        "sql": "SELECT 1;",
        "answer": "One row.",
        "confidence": 1.0,
        "needs_clarification": False,
    }


def test_structured_sql_normalizes_llama_cpp_sql_alias_response():
    client = LlamaCppClient(model="local-model", host="http://localhost:8080")
    # This is the malformed response shape reproduced from the running server.
    content = """```sql
SELECT
  'sql' AS sql,
  'SELECT * FROM salaries WHERE salary > 90000' AS answer,
  1 AS confidence,
  FALSE AS needs_clarification;
```"""
    with patch("requests.post", return_value=_response(content)):
        decision = client.generate_structured_sql("schema prompt")
    assert decision == {
        "sql": "SELECT * FROM salaries WHERE salary > 90000",
        "answer": "SQL query generated.",
        "confidence": 1.0,
        "needs_clarification": False,
    }


def test_explain_sql_uses_openai_compatible_chat_completion():
    client = LlamaCppClient(model="local-model", host="http://localhost:8080")
    with patch("requests.post", return_value=_response("Counts employees.")):
        assert client.explain_sql("SELECT COUNT(*) FROM employees;") == "Counts employees."
