from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[2] / "templates" / "index.html"
)


def test_chat_template_restores_one_active_tab_session():
    """Sidebar navigation must reuse the active browser-tab chat thread."""
    source = TEMPLATE.read_text(encoding="utf-8")

    assert "const CHAT_STATE_KEY = 'ai_database_assistant.active_chat.v1';" in source
    assert "let THREAD_ID = persistedChat.thread_id || newThreadId();" in source
    assert "messages: chatEntries.slice(-MAX_PERSISTED_MESSAGES)" in source
    assert "function restoreChat()" in source
    assert "restoreChat();" in source


def test_new_session_is_the_only_code_path_that_replaces_the_thread_id():
    """A fresh backend memory thread is created only from the explicit UI action."""
    source = TEMPLATE.read_text(encoding="utf-8")
    start_new_chat = source.split("function startNewChat()", 1)[1].split(
        "function useSamplePrompt", 1
    )[0]

    assert "THREAD_ID = newThreadId();" in start_new_chat
    assert "chatEntries = [];" in start_new_chat
    assert "persistChat();" in start_new_chat
