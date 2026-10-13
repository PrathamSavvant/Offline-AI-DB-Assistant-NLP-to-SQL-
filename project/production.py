"""
Production WSGI Server Entrypoint.
Boots the AI Database Assistant behind a multi-threaded Waitress WSGI server
with graceful signal interception (SIGINT/SIGTERM), automated preflight verification,
and clean resource teardown.

Usage:
    python production.py [--host 0.0.0.0] [--port 5000] [--threads 8]
"""
from __future__ import annotations

import argparse
import signal
import sys
from typing import Any

from config.settings import settings
from core.logger import get_logger, flush_logs
from preflight import run_preflight

logger = get_logger("production")


def _setup_signal_handlers(app: Any) -> None:
    """Register graceful termination hooks on SIGINT and SIGTERM."""
    def _handle_exit(sig: int, frame: Any) -> None:
        signame = signal.Signals(sig).name if hasattr(signal, "Signals") else str(sig)
        logger.info("Received termination signal %s. Initiating graceful shutdown...", signame)
        try:
            if hasattr(app, "shutdown"):
                app.shutdown()
        except Exception as exc:
            logger.error("Error during application shutdown: %s", exc)
        finally:
            flush_logs()
            sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _handle_exit)
        signal.signal(signal.SIGTERM, _handle_exit)
    except Exception as sig_err:
        logger.warning("Could not register standard signal handlers: %s", sig_err)


def _print_banner(host: str, port: int, threads: int) -> None:
    border = "=" * 68
    print(border)
    print("   AI Database Assistant & Understanding Engine - Production Server")
    print(border)
    print(f"[*] Environment:     {settings.ENVIRONMENT}")
    print(f"[*] WSGI Server:     Waitress (threads: {threads})")
    print(f"[*] Listening on:    http://{host}:{port}")
    print(f"[*] Active Database: {settings.DB_USER}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
    print(f"[*] LLM Endpoint:    {settings.LLM_ENDPOINT} (model: {settings.LLM_MODEL})")
    print(f"[*] Health Probes:   /healthz (liveness), /readyz (readiness), /api/health")
    print(f"[*] Log Level:       {settings.LOG_LEVEL}")
    print(border)
    print("Server initialized and ready to handle requests. Press Ctrl+C to stop.")
    print(border)


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Database Assistant Production WSGI Server")
    parser.add_argument("--host", default=settings.SERVER_HOST, help="Host address to bind to")
    parser.add_argument("--port", type=int, default=settings.SERVER_PORT, help="Port to bind to")
    parser.add_argument("--threads", type=int, default=settings.WAITRESS_THREADS, help="Waitress worker threads")
    parser.add_argument("--connection-limit", type=int, default=settings.WAITRESS_CONNECTION_LIMIT, help="Max connection limit")
    parser.add_argument("--channel-timeout", type=int, default=settings.WAITRESS_CHANNEL_TIMEOUT, help="Channel timeout in seconds")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip startup preflight checks")
    args = parser.parse_args()

    # 1. Run Preflight
    if not args.skip_preflight:
        logger.info("Executing preflight checks prior to server startup...")
        preflight_results = run_preflight(check_db=False, check_llm_server=False)
        if not preflight_results["passed"]:
            logger.error("Production preflight failed: %s", preflight_results["errors"])
            print("\n[ERROR] Production preflight failed:", file=sys.stderr)
            for err in preflight_results["errors"]:
                print(f"  - {err}", file=sys.stderr)
            return 1

    # 2. Import application factory
    from app import app
    from waitress import serve

    # 3. Register graceful shutdown handlers
    _setup_signal_handlers(app)

    # 4. Display startup banner
    _print_banner(args.host, args.port, args.threads)

    # 5. Serve application via Waitress
    try:
        serve(
            app,
            host=args.host,
            port=args.port,
            threads=args.threads,
            connection_limit=args.connection_limit,
            channel_timeout=args.channel_timeout,
            ident="AIDatabaseAssistant/1.0",
        )
    except (KeyboardInterrupt, SystemExit):
        logger.info("Server terminated by operator.")
    finally:
        if hasattr(app, "shutdown"):
            app.shutdown()
        flush_logs()

    return 0


if __name__ == "__main__":
    sys.exit(main())
