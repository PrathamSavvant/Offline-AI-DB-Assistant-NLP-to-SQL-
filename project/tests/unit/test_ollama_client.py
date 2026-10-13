from unittest.mock import MagicMock, patch
import pytest
import requests

from core.ollama_client import OllamaClient


def test_health_check_success():
    client = OllamaClient(model="qwen3:latest", host="http://localhost:11434")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "qwen3:latest"}]}

    with patch("requests.get", return_value=mock_resp) as mock_get:
        client.health_check()
        mock_get.assert_called_once_with("http://localhost:11434/api/tags", timeout=5)


def test_health_check_connection_error():
    client = OllamaClient(model="qwen3:latest", host="http://localhost:11434")

    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("Connection refused")):
        with pytest.raises(RuntimeError, match="Cannot reach Ollama at http://localhost:11434"):
            client.health_check()


def test_health_check_missing_model():
    client = OllamaClient(model="nonexistent_model:latest", host="http://localhost:11434")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "qwen3:latest"}]}

    with patch("requests.get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="Model 'nonexistent_model:latest' is not installed in Ollama"):
            client.health_check()
