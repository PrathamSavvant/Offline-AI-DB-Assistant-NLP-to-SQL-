"""
agent/memory.py

Lightweight thread-safe session memory.

Stores per-thread:
  - Last 10 conversation messages (user + assistant)
  - Current database context
  - Last SQL generated
  - Last table used

Does NOT build long-term memory. Sessions are in-process only.
"""

from collections import deque
import threading
from typing import Any, Dict, List, Optional


class MemoryStore:
    MAX_MESSAGES = 10  # Keep only last 10 messages per thread

    def __init__(self):
        # thread_id -> deque of {role, content}
        self._threads: Dict[str, deque] = {}
        # thread_id -> lightweight context dict
        self._context: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get_history(self, thread_id: str) -> List[Dict[str, str]]:
        with self._lock:
            return [message.copy() for message in self._threads.get(thread_id, [])]

    def add_message(self, thread_id: str, role: str, content: str) -> None:
        with self._lock:
            if thread_id not in self._threads:
                self._threads[thread_id] = deque(maxlen=self.MAX_MESSAGES)
            self._threads[thread_id].append({"role": role, "content": content})

    def update_context(self, thread_id: str, **kwargs) -> None:
        """Store lightweight session context (last_sql, last_table, etc.)."""
        with self._lock:
            if thread_id not in self._context:
                self._context[thread_id] = {}
            self._context[thread_id].update(kwargs)

    def get_context(self, thread_id: str) -> Dict[str, Any]:
        with self._lock:
            return self._context.get(thread_id, {}).copy()

    def clear(self, thread_id: str) -> None:
        with self._lock:
            self._threads.pop(thread_id, None)
            self._context.pop(thread_id, None)

    def clear_all(self) -> None:
        """Purge all threads and session contexts (used during database switches)."""
        with self._lock:
            self._threads.clear()
            self._context.clear()


memory_store = MemoryStore()
