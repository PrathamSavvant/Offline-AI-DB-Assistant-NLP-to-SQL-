"""
core/ollama_client.py

Persistent Ollama client. Initialized ONCE at Flask startup and reused
for every request. Never recreated mid-flight.

Supports:
- Configurable model (from settings, not hardcoded)
- One retry on timeout
- Immediate friendly error on any failure (never hangs)
- SQL extraction from model response
"""

import json
import re
import time
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger(__name__)


class OllamaServerError(RuntimeError):
    """A temporary Ollama daemon/model-runner failure eligible for one retry."""

# Singleton — set by app.py at startup
_instance: Optional["OllamaClient"] = None


def get_client() -> "OllamaClient":
    """Return the singleton OllamaClient. Raises if not initialized."""
    if _instance is None:
        raise RuntimeError("OllamaClient has not been initialized. Call init_client() at startup.")
    return _instance


def init_client(model: str, host: str = "http://localhost:11434", timeout: int = 120, temperature: float = 0.1) -> "OllamaClient":
    """Initialize and store the singleton OllamaClient. Call once at startup."""
    global _instance
    _instance = OllamaClient(model=model, host=host, timeout=timeout, temperature=temperature)
    return _instance


class OllamaClient:
    """
    Thin, persistent HTTP client for a local Ollama server.
    One instance is created at startup and reused for all requests.

    Usage:
        client = init_client(model="qwen3:latest")
        sql = client.generate_sql(prompt)
    """

    def __init__(
        self,
        model: str,  # No default — must be configured explicitly
        host: str = "http://localhost:11434",
        timeout: int = 120,
        temperature: float = 0.1,
        think: bool = False,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.think = think

    # ------------------------------------------------------------------
    # HEALTH CHECK
    # ------------------------------------------------------------------

    def health_check(self) -> None:
        """
        Verify Ollama is running and the configured model exists.
        Raises RuntimeError with a clear message if either check fails.
        Called once at application startup.
        """
        # 1. Is Ollama reachable?
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=5)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot reach Ollama at {self.host}. "
                "Please start it with: ollama serve"
            )
        except Exception as e:
            raise RuntimeError(f"Ollama health check failed: {e}")

        # 2. Does the configured model exist?
        models = [m["name"] for m in resp.json().get("models", [])]
        # Match on base name or full name (e.g. "qwen3:latest" or "qwen3")
        base = self.model.split(":")[0]
        found = any(
            m == self.model or m.split(":")[0] == base
            for m in models
        )
        if not found:
            raise RuntimeError(
                f"Model '{self.model}' is not installed in Ollama. "
                f"Available models: {models or ['(none)']}\n"
                f"Install it with: ollama pull {self.model}"
            )

        logger.info("Ollama health check passed. Model '%s' is available.", self.model)

    # ------------------------------------------------------------------
    # GENERATION
    # ------------------------------------------------------------------

    def generate_sql(self, prompt: str) -> str:
        """
        Send prompt to Ollama and return extracted SQL.
        Retries once on timeout.
        Raises RuntimeError with a clear message on any failure.
        Maximum ONE LLM call per request (two only if first times out).
        """
        return self._call_with_retry(prompt)

    def generate_structured_sql(self, prompt: str) -> dict:
        """Return an auditable JSON decision, never a best-effort SQL scrape.

        Ollama's JSON mode is requested explicitly and the response is still
        validated locally because model output is untrusted input.
        """
        contract = (
            "\nReturn JSON only with keys: sql (string or null), "
            "answer (string), confidence (number 0..1), needs_clarification (boolean). "
            "Use sql=null when the cached schema cannot answer safely."
        )
        raw = self._call_with_retry(prompt + contract, extract_sql=False, json_mode=True)
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid structured SQL output; no query was executed.") from exc
        if not isinstance(value, dict) or not isinstance(value.get("answer"), str):
            raise RuntimeError("Ollama structured SQL output did not match the required contract; no query was executed.")
        sql = value.get("sql")
        if sql is not None and not isinstance(sql, str):
            raise RuntimeError("Ollama structured SQL output has an invalid sql field; no query was executed.")
        value["confidence"] = float(value.get("confidence", 0))
        value["needs_clarification"] = bool(value.get("needs_clarification", sql is None))
        return value

    def generate_explanation(self, prompt: str) -> str:
        """Generate a concise explanation without applying SQL extraction."""
        return self._call_with_retry(prompt, extract_sql=False)

    def explain_sql(self, sql: str) -> str:
        return self.generate_explanation(f"Explain this SQL concisely and accurately:\n```sql\n{sql}\n```")

    def _call_with_retry(self, prompt: str, extract_sql: bool = True, json_mode: bool = False) -> str:
        """Try once; if timeout, retry once; otherwise surface error immediately."""
        try:
            return self._call(prompt, extract_sql=extract_sql, json_mode=json_mode)
        except (requests.exceptions.Timeout, OllamaServerError) as first_error:
            logger.warning("Ollama failed transiently (%s). Retrying once...", first_error)
            try:
                return self._call(prompt, extract_sql=extract_sql, json_mode=json_mode)
            except (requests.exceptions.Timeout, OllamaServerError) as second_error:
                raise RuntimeError(
                    "Ollama could not generate a response after two attempts. "
                    f"The local model runner reported: {second_error}"
                ) from second_error
            except Exception as e:
                raise RuntimeError(f"Ollama failed on retry: {e}") from e

    def _call(self, prompt: str, extract_sql: bool = True, json_mode: bool = False) -> str:
        """Single HTTP call to Ollama /api/generate."""
        url = f"{self.host}/api/generate"
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": self.think,  # False disables qwen3 internal chain-of-thought
            "options": {"temperature": self.temperature},
        }
        if json_mode:
            payload["format"] = "json"

        logger.info("Calling Ollama model=%s prompt_len=%d", self.model, len(prompt))
        t0 = time.perf_counter()

        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(
                f"Could not reach Ollama at {self.host}. Is `ollama serve` running? ({e})"
            ) from e
        except requests.exceptions.Timeout:
            raise  # Let _call_with_retry handle
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            detail = ""
            if e.response is not None:
                try:
                    detail = e.response.text.strip()[:500]
                except Exception:
                    pass
            message = f"Ollama returned HTTP {status or 'unknown'}"
            if detail:
                message += f": {detail}"
            if status and status >= 500:
                raise OllamaServerError(message) from e
            raise RuntimeError(message) from e

        elapsed = time.perf_counter() - t0
        logger.info("Ollama responded in %.2fs", elapsed)

        data = response.json()
        raw_text = data.get("response", "")
        if not raw_text.strip():
            raise RuntimeError("Ollama returned an empty response.")

        result = self.extract_sql(raw_text) if extract_sql else raw_text.strip()
        logger.info("Ollama generated response (%d characters)", len(result))
        return result

    # ------------------------------------------------------------------
    # SQL EXTRACTION
    # ------------------------------------------------------------------

    @staticmethod
    def extract_sql(raw_text: str) -> str:
        """
        Pull a clean SQL statement from the model's raw text response.
        Handles:
        - SQL wrapped in ```sql ... ``` or ``` ... ``` fences
        - SQL preceded by explanatory text
        - Trailing commentary after statement
        - <think>...</think> blocks (qwen3 reasoning tokens)
        """
        # Strip qwen3 thinking/reasoning blocks
        text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()

        fence_match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        candidate = fence_match.group(1).strip() if fence_match else text.strip()

        start_match = re.search(r"\b(SELECT|WITH)\b", candidate, re.IGNORECASE)
        if start_match:
            candidate = candidate[start_match.start():]

        semi_index = candidate.find(";")
        if semi_index != -1:
            candidate = candidate[: semi_index + 1]

        return candidate.strip()
