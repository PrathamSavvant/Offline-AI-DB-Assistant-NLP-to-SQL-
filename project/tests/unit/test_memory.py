import threading
from agent.memory import MemoryStore


def test_memory_store_handles_concurrent_writes():
    store = MemoryStore()
    workers = [threading.Thread(target=store.add_message, args=("thread", "user", str(index))) for index in range(20)]
    for worker in workers: worker.start()
    for worker in workers: worker.join()
    assert len(store.get_history("thread")) == store.MAX_MESSAGES


def test_context_is_returned_as_a_copy():
    store = MemoryStore(); store.update_context("thread", last_sql="SELECT 1")
    context = store.get_context("thread"); context["last_sql"] = "changed"
    assert store.get_context("thread")["last_sql"] == "SELECT 1"
