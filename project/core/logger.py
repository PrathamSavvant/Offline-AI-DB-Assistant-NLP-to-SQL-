"""
core/logger.py

Centralized logging configuration for the AI Database Understanding Engine.

All modules should obtain their logger via `get_logger(__name__)` rather than
configuring logging themselves. This keeps log formatting and output location
consistent across the whole project.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "engine.log")

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _get_configured_level() -> int:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def _initialize_logging() -> None:
    """
    Configure the root logging handlers exactly once for the whole process.
    Subsequent calls are no-ops.
    """

    global _initialized

    if _initialized:
        return

    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    max_bytes = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
    backup_count = int(os.getenv("LOG_BACKUP_COUNT", 5))
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    configured_level = _get_configured_level()
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(configured_level)

    root_logger = logging.getLogger()
    root_logger.setLevel(min(configured_level, logging.DEBUG))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    _initialized = True


def flush_logs() -> None:
    """Flush all logging handlers to disk."""
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass


def get_logger(name: str) -> logging.Logger:
    """
    Return a module-level logger. Ensures logging is initialized before
    any logger is handed out.

    Usage:
        from core.logger import get_logger
        logger = get_logger(__name__)
    """

    _initialize_logging()
    return logging.getLogger(name)