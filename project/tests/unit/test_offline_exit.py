"""The web exit must never stop arbitrary or non-bundle server processes."""
import json
import os
import sys
from pathlib import Path

from flask import Flask, render_template

from routes import offline_exit


def _client(monkeypatch, *, available=True):
    app = Flask(__name__)
    app.secret_key = "test-only-secret"
    app.register_blueprint(offline_exit.bp)
    monkeypatch.setattr(offline_exit, "_owned_offline_app", lambda: available)
    return app.test_client()


def test_exit_requires_local_session_token(monkeypatch):
    client = _client(monkeypatch)
    assert client.post("/api/offline/exit", json={}).status_code == 403
    with client.session_transaction() as session:
        session["offline_exit_token"] = "correct-token"
    assert client.post("/api/offline/exit", json={}, headers={"X-Exit-Token": "wrong"}).status_code == 403
    assert client.post("/api/offline/exit", data="", headers={"X-Exit-Token": "correct-token"}).status_code == 403
    assert client.post("/api/offline/exit", json={}, headers={"X-Exit-Token": "correct-token"},
                       environ_base={"REMOTE_ADDR": "192.0.2.1"}).status_code == 403


def test_exit_launches_existing_shutdown_script_only_after_valid_request(monkeypatch):
    launched = []
    monkeypatch.setattr(offline_exit.subprocess, "Popen", lambda *a, **kw: launched.append((a, kw)))
    client = _client(monkeypatch)
    with client.session_transaction() as session:
        session["offline_exit_token"] = "correct-token"
    result = client.post("/api/offline/exit", json={}, headers={"X-Exit-Token": "correct-token"})
    assert result.status_code == 200
    assert result.json["status"] == "stopping"
    assert result.json["scope"] == "application_and_model"
    assert len(launched) == 1
    assert str(offline_exit.SHUTDOWN_SCRIPT) in launched[0][0][0]
    assert "-DelaySeconds" in launched[0][0][0]


def test_manual_exit_stops_only_current_application(monkeypatch):
    stopped = []
    client = _client(monkeypatch, available=False)
    monkeypatch.setattr(offline_exit, "_schedule_self_stop", lambda app: stopped.append(app))
    with client.session_transaction() as session:
        session["offline_exit_token"] = "correct-token"
    result = client.post("/api/offline/exit", json={}, headers={"X-Exit-Token": "correct-token"})
    assert result.status_code == 200
    assert result.json["scope"] == "application_only"
    assert len(stopped) == 1


def test_exit_hidden_if_state_does_not_own_current_process(monkeypatch, tmp_path):
    state_path = tmp_path / "offline-processes.json"
    script = tmp_path / "offline_shutdown.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(offline_exit, "STATE_PATH", state_path)
    monkeypatch.setattr(offline_exit, "SHUTDOWN_SCRIPT", script)
    monkeypatch.setenv("OFFLINE_LAUNCHER", "1")
    state_path.write_text(json.dumps({"application_pid": os.getpid() + 1,
                                      "application_executable": sys.executable}), encoding="utf-8")
    assert not offline_exit._owned_offline_app()
    state_path.write_text(json.dumps({"application_pid": os.getpid(),
                                      "application_executable": sys.executable}), encoding="utf-8")
    assert offline_exit._owned_offline_app() == (os.name == "nt")


def test_exit_button_renders_for_local_manual_or_offline_app(monkeypatch):
    app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[2] / "templates"))
    app.secret_key = "test-only-secret"
    app.register_blueprint(offline_exit.bp)

    @app.get("/")
    def page():
        return render_template("_app_shell.html")

    client = app.test_client()
    monkeypatch.setattr(offline_exit, "_owned_offline_app", lambda: False)
    assert b'data-exit-mode="application"' in client.get("/").data
    monkeypatch.setattr(offline_exit, "_owned_offline_app", lambda: True)
    assert b'data-exit-mode="bundle"' in client.get("/").data
    assert b'id="exitApplication"' not in client.get("/", environ_base={"REMOTE_ADDR": "192.0.2.1"}).data
