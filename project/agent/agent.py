"""Stateful HTTP/chat-session facade over the two-agent SQL pipeline."""
import threading
import uuid
from typing import Dict, Generator, Optional
from agent.memory import memory_store

class DatabaseSessionFacade:
    def __init__(self): self.engine = None; self.memory = memory_store; self._lock = threading.RLock()
    def configure(self, engine):
        with self._lock: self.engine = engine
    def _engine(self):
        with self._lock:
            if self.engine is None: raise RuntimeError("Database session facade has not been configured by create_app().")
            return self.engine
    def run(self, question: str, thread_id: Optional[str] = None) -> Dict:
        tid = thread_id or str(uuid.uuid4()); self.memory.add_message(tid, "user", question)
        result = self._engine().execute(question, tid); result["thread_id"] = tid
        self.memory.update_context(tid, last_sql=result.get("sql"))
        self.memory.add_message(tid, "assistant", result.get("final_response", "")); return result
    def explain_sql(self, thread_id: Optional[str] = None) -> Dict:
        sql = self.memory.get_context(thread_id).get("last_sql") if thread_id else None
        sql = sql or (self._engine().last_prompt_info or {}).get("sql")
        if not sql:
            raise ValueError("There is no previously generated SQL to explain.")
        engine = self._engine()
        explanation = engine.llama_agent.explain(sql) if engine.llama_agent is not None else engine.llm.explain_sql(sql)
        return {"intent": "EXPLANATION", "sql": sql, "final_response": explanation, "suggested_followups": []}
    def stream(self, question: str, thread_id: Optional[str] = None) -> Generator[Dict, None, None]:
        tid = thread_id or str(uuid.uuid4()); self.memory.add_message(tid, "user", question)
        for event in self._engine().stream(question, tid):
            if event["event"] == "message":
                self.memory.update_context(tid, last_sql=event["data"].get("sql"))
                self.memory.add_message(tid, "assistant", event["data"].get("final_response", ""))
            yield event

# Backward-compatible class/instance names for existing routes and imports.
DatabaseAgent = DatabaseSessionFacade
database_agent = DatabaseSessionFacade()
