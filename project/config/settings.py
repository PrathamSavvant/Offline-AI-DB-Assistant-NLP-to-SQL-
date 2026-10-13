"""
config/settings.py

Loads database connection configuration from environment variables (.env).
No credentials or database names are ever hardcoded — this is what lets the
engine run against any MySQL database just by changing .env.
"""

import os
import secrets

from dotenv import load_dotenv

load_dotenv()


class Settings:
    DB_HOST: str = os.getenv("DB_HOST", "localhost")
    DB_PORT: int = int(os.getenv("DB_PORT", 3306))
    DB_USER: str = os.getenv("DB_USER", "root")
    DB_PASSWORD: str = os.getenv("DB_PASSWORD", "")
    DB_NAME: str = os.getenv("DB_NAME", "")
    DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", 5))
    DB_CONNECT_TIMEOUT: int = int(os.getenv("DB_CONNECT_TIMEOUT", 10))
    DB_READ_TIMEOUT: int = int(os.getenv("DB_READ_TIMEOUT", 30))
    DB_QUERY_TIMEOUT_MS: int = int(os.getenv("DB_QUERY_TIMEOUT_MS", 30000))
    # EXPLAIN is run before user SQL. Set either limit to 0 only after DBA review.
    DB_MAX_ESTIMATED_SCAN_ROWS: int = int(os.getenv("DB_MAX_ESTIMATED_SCAN_ROWS", 2000000))
    DB_MAX_ESTIMATED_JOIN_ROWS: int = int(os.getenv("DB_MAX_ESTIMATED_JOIN_ROWS", 5000000))
    # Set from the DBA's inventory to catch a restricted MySQL account that
    # can see only part of the intended schema.
    DB_EXPECTED_MIN_TABLES: int = int(os.getenv("DB_EXPECTED_MIN_TABLES", 0))

    LLM_ENDPOINT: str = os.getenv("LLM_ENDPOINT", "http://localhost:8080")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "local-model")
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", 60))
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", 0.1))
    # Prevent local models from repeating join predicates until their output
    # budget is exhausted; validated against the CPU llama.cpp deployment.
    LLM_REPEAT_PENALTY: float = float(os.getenv("LLM_REPEAT_PENALTY", 1.15))
    # SQL decisions are short JSON objects. A hard output cap prevents a
    # CPU-only local model from spending unbounded time generating prose.
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", 384))
    # Scale controls: keep every request bounded even when a database has
    # thousands of tables/columns or individual tables have millions of rows.
    MAX_CONTEXT_TABLES: int = int(os.getenv("MAX_CONTEXT_TABLES", 4))
    MAX_CONTEXT_COLUMNS_PER_TABLE: int = int(os.getenv("MAX_CONTEXT_COLUMNS_PER_TABLE", 12))
    MAX_PROMPT_CHARS: int = int(os.getenv("MAX_PROMPT_CHARS", 18000))
    # Bounded deterministic retrieval for large schemas. These settings cap
    # candidates before the existing final table limit applies.
    CONTEXT_CANDIDATE_LIMIT: int = int(os.getenv("CONTEXT_CANDIDATE_LIMIT", 12))
    CONTEXT_RELATIONSHIP_NEIGHBOR_LIMIT: int = int(os.getenv("CONTEXT_RELATIONSHIP_NEIGHBOR_LIMIT", 2))
    MAX_RETRIEVAL_CANDIDATES: int = int(os.getenv("MAX_RETRIEVAL_CANDIDATES", 30))
    MAX_FINAL_TABLES: int = int(os.getenv("MAX_FINAL_TABLES", os.getenv("MAX_CONTEXT_TABLES", 4)))
    SEMANTIC_CONTEXT_MAX_CHARS: int = int(os.getenv("SEMANTIC_CONTEXT_MAX_CHARS", 480))
    MAX_PRECOMPUTED_JOIN_TABLES: int = int(os.getenv("MAX_PRECOMPUTED_JOIN_TABLES", 100))
    SQL_ROW_LIMIT: int = int(os.getenv("SQL_ROW_LIMIT", 200))
    # Observed categorical values may change during the working day. Keep
    # lookups fresh without re-crawling a large schema on every row update.
    VALUE_LOOKUP_CACHE_TTL_SECONDS: int = int(os.getenv("VALUE_LOOKUP_CACHE_TTL_SECONDS", 120))
    VALUE_LOOKUP_CACHE_MAX_ENTRIES: int = int(os.getenv("VALUE_LOOKUP_CACHE_MAX_ENTRIES", 256))
    ENABLE_EMBEDDINGS: bool = os.getenv("ENABLE_EMBEDDINGS", "false").lower() in {"1", "true", "yes"}
    EMBEDDING_TOP_K: int = int(os.getenv("EMBEDDING_TOP_K", 20))
    ENABLE_BACKGROUND_SYNC: bool = os.getenv("ENABLE_BACKGROUND_SYNC", "true").lower() in {"1", "true", "yes"}
    SYNC_INTERVAL_MINUTES: int = int(os.getenv("SYNC_INTERVAL_MINUTES", 15))

    # Production Server & Process Settings
    ENVIRONMENT: str = os.getenv("FLASK_ENV", os.getenv("ENVIRONMENT", "production")).lower()
    SECRET_KEY: str = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
    SERVER_HOST: str = os.getenv("SERVER_HOST", "127.0.0.1")
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", os.getenv("PORT", 5000)))
    WAITRESS_THREADS: int = int(os.getenv("WAITRESS_THREADS", os.getenv("SERVER_THREADS", 8)))
    WAITRESS_CONNECTION_LIMIT: int = int(os.getenv("WAITRESS_CONNECTION_LIMIT", 100))
    WAITRESS_CHANNEL_TIMEOUT: int = int(os.getenv("WAITRESS_CHANNEL_TIMEOUT", 120))
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "")
    MAX_CONTENT_LENGTH: int = int(os.getenv("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))

    # Observability & Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_MAX_BYTES: int = int(os.getenv("LOG_MAX_BYTES", 10 * 1024 * 1024))
    LOG_BACKUP_COUNT: int = int(os.getenv("LOG_BACKUP_COUNT", 5))
    ENABLE_SENSITIVE_DEBUG: bool = os.getenv("ENABLE_SENSITIVE_DEBUG", "false").lower() in {"1", "true", "yes"}

    def is_production(self) -> bool:
        return self.ENVIRONMENT in {"production", "prod"}

    def to_safe_dict(self) -> dict:
        """Return non-sensitive settings suitable for logging and status endpoints."""
        return {
            "environment": self.ENVIRONMENT,
            "db_host": self.DB_HOST,
            "db_port": self.DB_PORT,
            "db_name": self.DB_NAME,
            "db_user": self.DB_USER,
            "db_pool_size": self.DB_POOL_SIZE,
            "db_expected_min_tables": self.DB_EXPECTED_MIN_TABLES,
            "llm_endpoint": self.LLM_ENDPOINT,
            "llm_model": self.LLM_MODEL,
            "max_context_tables": self.MAX_CONTEXT_TABLES,
            "sql_row_limit": self.SQL_ROW_LIMIT,
            "background_sync_enabled": self.ENABLE_BACKGROUND_SYNC,
            "sync_interval_minutes": self.SYNC_INTERVAL_MINUTES,
            "server_host": self.SERVER_HOST,
            "server_port": self.SERVER_PORT,
            "server_threads": self.WAITRESS_THREADS,
            "cors_origins": self.CORS_ORIGINS,
            "log_level": self.LOG_LEVEL,
        }

    def validate(self) -> None:
        """
        Fail fast and loudly if required settings are missing, instead of
        letting the connector raise a cryptic error later.
        """

        missing = []

        if not self.DB_NAME:
            missing.append("DB_NAME")
        if not self.DB_USER:
            missing.append("DB_USER")
        if not self.DB_HOST:
            missing.append("DB_HOST")

        if missing:
            raise ValueError(
                f"Missing required setting(s) in .env: {', '.join(missing)}"
            )


settings = Settings()
