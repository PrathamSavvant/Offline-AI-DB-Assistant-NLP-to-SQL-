"""Explicit, local-only exit for the app and its owned offline bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

from flask import Blueprint, current_app, request, session

from core.serializer import safe_jsonify


bp = Blueprint("offline_exit", __name__)
PROJECT_DIR = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_DIR / ".runtime" / "offline-processes.json"
SHUTDOWN_SCRIPT = PROJECT_DIR / "offline_shutdown.ps1"


def _owned_offline_app() -> bool:
    """Only the app started and recorded by this bundle may stop the model."""
    if os.environ.get("OFFLINE_LAUNCHER") != "1" or os.name != "nt":
        return False
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
        return (
            int(state.get("application_pid") or 0) == os.getpid()
            and Path(state.get("application_executable") or "").resolve()
            == Path(sys.executable).resolve()
            and SHUTDOWN_SCRIPT.is_file()
        )
    except (OSError, ValueError, TypeError):
        return False


@bp.app_context_processor
def offline_exit_template_context():
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return {"offline_exit_available": False}
    if "offline_exit_token" not in session:
        session["offline_exit_token"] = secrets.token_urlsafe(32)
    return {
        "offline_exit_available": True,
        "offline_exit_token": session["offline_exit_token"],
        "offline_exit_mode": "bundle" if _owned_offline_app() else "application",
    }


def _schedule_self_stop(app):
    """Let the HTTP response flush before terminating a manually started app."""
    def stop():
        time.sleep(1)
        try:
            if hasattr(app, "shutdown"):
                app.shutdown()
        finally:
            os._exit(0)

    threading.Thread(target=stop, name="manual-app-exit", daemon=True).start()


@bp.post("/api/offline/exit")
def exit_offline_application():
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return safe_jsonify(error="Exit is only available from this computer."), 403
    expected = session.get("offline_exit_token", "")
    supplied = request.headers.get("X-Exit-Token", "")
    if not request.is_json or not expected or not secrets.compare_digest(supplied, expected):
        return safe_jsonify(error="Invalid exit request."), 403
    if _owned_offline_app():
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-WindowStyle", "Hidden", "-File", str(SHUTDOWN_SCRIPT), "-DelaySeconds", "1"],
                cwd=str(PROJECT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return safe_jsonify(error="Could not start shutdown. Use the Stop desktop shortcut."), 500
        return safe_jsonify(status="stopping", scope="application_and_model")
    _schedule_self_stop(current_app._get_current_object())
    return safe_jsonify(status="stopping", scope="application_only")
