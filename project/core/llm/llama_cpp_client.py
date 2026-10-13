"""llama.cpp client using its OpenAI-compatible HTTP API."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict

import requests

from core.llm.base import BaseLLM
from core.logger import get_logger

logger = get_logger(__name__)


class LlamaCppServerError(RuntimeError):
    """Temporary llama.cpp server failure eligible for one retry."""


class LlamaCppStructuredOutputUnsupported(RuntimeError):
    """The server rejected the OpenAI-compatible JSON Schema request."""


class LlamaCppClient(BaseLLM):
    """Adapter for a llama.cpp server exposing ``/health`` and OpenAI v1 APIs."""

    # This is the provider-side form of the existing SQL decision contract.  Keep
    # it aligned with downstream fields if that contract ever changes.
    SQL_DECISION_SCHEMA = {
        "type": "object",
        "properties": {
            "sql": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "answer": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "needs_clarification": {"type": "boolean"},
        },
        "required": ["sql", "answer", "confidence", "needs_clarification"],
        "additionalProperties": False,
    }

    def __init__(self, model: str, host: str = "http://localhost:8080", timeout: int = 120,
                 temperature: float = 0.1, max_tokens: int = 384, repeat_penalty: float = 1.15):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max(1, max_tokens)
        self.repeat_penalty = max(0.01, repeat_penalty)
        self._structured_output_supported: bool | None = None

    def health_check(self) -> None:
        try:
            response = requests.get(f"{self.host}/health", timeout=5)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"Cannot reach llama.cpp server at {self.host}: {exc}") from exc

    def generate_structured_sql(self, prompt: str) -> Dict[str, Any]:
        content = self._chat(prompt, structured_output=True)
        logger.debug("llama.cpp structured SQL text received (%d characters)", len(content))
        try:
            # Normal path: llama.cpp enforces this object through JSON Schema.
            decision = json.loads(content)
            logger.debug("llama.cpp native JSON Schema output accepted.")
        except json.JSONDecodeError as exc:
            # Retain compatibility for servers/models that unexpectedly ignore or
            # reject structured output.  Downstream code remains provider-agnostic.
            logger.warning(
                "llama.cpp native structured output was invalid; using compatibility parser (%s)",
                type(exc).__name__,
            )
            try:
                decision = self._normalize_structured_sql(content)
            except json.JSONDecodeError as fallback_error:
                raise RuntimeError("llama.cpp returned invalid structured SQL output; no query was executed.") from fallback_error
        if not isinstance(decision, dict) or not isinstance(decision.get("answer"), str):
            raise RuntimeError("llama.cpp structured SQL output did not match the required contract; no query was executed.")
        if decision.get("sql") is not None and not isinstance(decision["sql"], str):
            raise RuntimeError("llama.cpp structured SQL output has an invalid sql field; no query was executed.")
        decision["confidence"] = float(decision.get("confidence", 0))
        decision["needs_clarification"] = bool(decision.get("needs_clarification", decision.get("sql") is None))
        logger.debug("llama.cpp normalized structured SQL response (has_sql=%s)", bool(decision.get("sql")))
        return decision

    def explain_sql(self, sql: str) -> str:
        return self._chat(f"Explain this SQL concisely and accurately:\n```sql\n{sql}\n```")

    def _chat(self, prompt: str, structured_output: bool = False) -> str:
        try:
            return self._request_chat(prompt, structured_output=structured_output)
        except LlamaCppStructuredOutputUnsupported as exc:
            # Do not fail a working local deployment if a future/older server does
            # not implement JSON Schema.  The legacy parser remains the fallback.
            self._structured_output_supported = False
            logger.warning("llama.cpp JSON Schema output is unavailable (%s); using compatibility fallback.", exc)
            return self._request_chat(prompt, structured_output=False)
        except (requests.exceptions.Timeout, LlamaCppServerError) as first_error:
            logger.warning("llama.cpp failed transiently (%s). Retrying once...", first_error)
            try:
                return self._request_chat(prompt, structured_output=structured_output)
            except (requests.exceptions.Timeout, LlamaCppServerError) as second_error:
                raise RuntimeError(
                    "llama.cpp could not generate a response after two attempts. "
                    f"The local model server reported: {second_error}"
                ) from second_error

    def _request_chat(self, prompt: str, structured_output: bool = False) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "repeat_penalty": self.repeat_penalty,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if structured_output and self._structured_output_supported is not False:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sql_decision",
                    "strict": True,
                    "schema": self.SQL_DECISION_SCHEMA,
                },
            }
            logger.debug("llama.cpp JSON Schema structured output is enabled.")
        try:
            started = time.perf_counter()
            response = requests.post(f"{self.host}/v1/chat/completions", json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            content = message["content"]
            logger.debug("Extracted llama.cpp assistant message (%d characters)", len(content) if isinstance(content, str) else 0)
            usage = data.get("usage", {})
            logger.info(
                "llama.cpp completion finished in %.2fs (prompt_tokens=%s, completion_tokens=%s, cap=%d)",
                time.perf_counter() - started,
                usage.get("prompt_tokens", "unknown"),
                usage.get("completion_tokens", "unknown"),
                self.max_tokens,
            )
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            message = f"llama.cpp returned HTTP {status or 'unknown'}"
            if structured_output and status in {400, 422}:
                raise LlamaCppStructuredOutputUnsupported(message) from exc
            if status and status >= 500:
                raise LlamaCppServerError(message) from exc
            raise RuntimeError(message) from exc
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"llama.cpp generation failed: {exc}") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("llama.cpp returned an empty response.")
        logger.info("llama.cpp responded using model=%s", self.model)
        return content.strip()

    @staticmethod
    def _normalize_structured_sql(content: str) -> Dict[str, Any]:
        """Adapt OpenAI-compatible llama.cpp text to Ollama JSON-mode output.

        The former Ollama call requested ``format=json`` and supplied the parser
        with only that JSON text.  llama.cpp's chat endpoint can instead return
        Markdown fences, surrounding assistant prose, or Qwen reasoning tags.
        This boundary-only compatibility layer extracts one JSON object without
        changing the prompt or any downstream SQL decision handling.
        """
        text = re.sub(
            r"<(?:think|analysis)>.*?(?:</(?:think|analysis)>|$)",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        candidate = fence.group(1).strip() if fence else text

        start = candidate.find("{")
        if start < 0:
            legacy_decision = LlamaCppClient._extract_sql_alias_decision(candidate)
            if legacy_decision is not None:
                return legacy_decision
            raise json.JSONDecodeError("No JSON object found", candidate, 0)
        value, _ = json.JSONDecoder().raw_decode(candidate[start:])
        if not isinstance(value, dict):
            return value

        # Some OpenAI-compatible chat templates use equivalent field names.
        # Canonicalize them here so callers retain the historical Ollama contract.
        aliases = {
            "query": "sql",
            "sql_query": "sql",
            "message": "answer",
            "response": "answer",
            "confidence_score": "confidence",
            "needsClarification": "needs_clarification",
        }
        for source, destination in aliases.items():
            if destination not in value and source in value:
                value[destination] = value.pop(source)
        return value

    @staticmethod
    def _extract_sql_alias_decision(content: str) -> Dict[str, Any] | None:
        """Recover the malformed ``SELECT '...' AS sql`` form from llama.cpp.

        With the existing SQL-focused prompt, some instruction-tuned models render
        the requested decision as a SQL row instead of a JSON object.  Ollama JSON
        mode prevented that form.  Treat this only as a provider boundary format:
        recover the quoted SQL value and return the unchanged historical contract.
        """
        fields = {
            name.lower(): value.replace("''", "'")
            for value, name in re.findall(
                r"'((?:''|[^'])*)'\s+AS\s+`?(sql|answer)`?\b",
                content,
                flags=re.IGNORECASE | re.DOTALL,
            )
        }
        sql = fields.get("sql", "").strip()
        answer = fields.get("answer", "").strip()

        # The reproduced llama.cpp output places the actual SQL in `answer` and
        # uses the literal string 'sql' for the `sql` alias.  Prefer whichever
        # quoted value is an actual read-only query.
        if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
            sql = answer if re.match(r"^(SELECT|WITH)\b", answer, re.IGNORECASE) else ""
        if not sql:
            fence = re.search(r"```(?:sql)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
            sql = fence.group(1).strip() if fence else ""
            if not re.match(r"^(SELECT|WITH)\b", sql, re.IGNORECASE):
                return None

        confidence = re.search(r"\b([01](?:\.\d+)?)\s+AS\s+`?confidence`?\b", content, re.IGNORECASE)
        clarification = re.search(r"\b(TRUE|FALSE)\s+AS\s+`?needs_clarification`?\b", content, re.IGNORECASE)
        return {
            "sql": sql,
            "answer": answer if answer and answer != sql else "SQL query generated.",
            "confidence": float(confidence.group(1)) if confidence else 0.0,
            "needs_clarification": clarification.group(1).lower() == "true" if clarification else False,
        }
