"""
core/database.py

Thin wrapper around mysql-connector-python that centralizes connection
handling, cursor creation, and query execution. All other modules
(scanner.py, etc.) talk to MySQL exclusively through this class — nothing
else imports mysql.connector directly.
"""

from abc import ABC, abstractmethod
import hashlib
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import mysql.connector
from mysql.connector import Error
from mysql.connector.connection import MySQLConnection
from mysql.connector import pooling

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)


class DatabaseAdapter(ABC):
    """Database boundary used by services; implementations may target any engine."""

    @abstractmethod
    def execute(self, query: str, params: Optional[Tuple[Any, ...]] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        """Release adapter resources. Stateless pooled adapters need no action."""


@dataclass(frozen=True)
class DatabaseConfig:
    """An immutable connection target captured when a runtime is activated."""
    host: str
    port: int
    user: str
    password: str
    database: str
    pool_size: int

    @classmethod
    def from_settings(cls) -> "DatabaseConfig":
        return cls(settings.DB_HOST, settings.DB_PORT, settings.DB_USER, settings.DB_PASSWORD,
                   settings.DB_NAME, settings.DB_POOL_SIZE)


class Database(DatabaseAdapter):
    """
    Manages a single MySQL connection and exposes simple helpers for
    getting cursors and running queries.
    """

    # Pools are deliberately isolated by connection target.  A database switch
    # can therefore never hand an old database connection to a new request.
    _pools: Dict[DatabaseConfig, pooling.MySQLConnectionPool] = {}
    _pool_lock = threading.RLock()
    # Backwards-compatible alias used by older integration code.
    _pool: Optional[pooling.MySQLConnectionPool] = None

    def __init__(self, config: Optional[DatabaseConfig] = None):
        self.connection: Optional[MySQLConnection] = None
        self.config = config or DatabaseConfig.from_settings()

    @classmethod
    def clear_pools(cls) -> None:
        """Forget pool references after a configuration change or shutdown."""
        with cls._pool_lock:
            cls._pools.clear()
            cls._pool = None

    @classmethod
    def get_pool_status(cls) -> Dict[str, Any]:
        """Return diagnostic metrics on active connection pools."""
        with cls._pool_lock:
            return {
                "active_pools": len(cls._pools),
                "configs": [
                    {"host": c.host, "port": c.port, "database": c.database, "pool_size": c.pool_size}
                    for c in cls._pools.keys()
                ],
            }

    def connect(self) -> MySQLConnection:
        """
        Establish the connection to MySQL using settings from .env.
        Raises an Exception with a clear message on failure.
        """

        settings.validate()

        try:
            with self.__class__._pool_lock:
                pool = self.__class__._pools.get(self.config)
                if pool is None:
                    digest = hashlib.sha1(
                        f"{self.config.host}:{self.config.port}:{self.config.user}:{self.config.database}".encode()
                    ).hexdigest()[:10]
                    pool = pooling.MySQLConnectionPool(
                        pool_name=f"db_assistant_{digest}", pool_size=self.config.pool_size,
                        host=self.config.host, port=self.config.port, user=self.config.user,
                        password=self.config.password, database=self.config.database,
                        connection_timeout=settings.DB_CONNECT_TIMEOUT,
                        read_timeout=settings.DB_READ_TIMEOUT,
                    )
                    self.__class__._pools[self.config] = pool
                self.__class__._pool = pool
            self.connection = pool.get_connection()

            if self.connection.is_connected():
                logger.info(
                    "Connected to MySQL database '%s' at %s:%s",
                    self.config.database, self.config.host, self.config.port,
                )
                return self.connection

        except Error as e:
            logger.error("Database connection failed: %s", e)
            raise Exception(f"Database Connection Error: {e}") from e

    def cursor(self, dictionary: bool = True):
        """
        Return a new cursor on the current connection, connecting first
        if necessary. dictionary=True returns rows as dicts (column_name
        -> value), which is what scanner.py expects throughout.
        """

        if self.connection is None or not self.connection.is_connected():
            self.connect()

        return self.connection.cursor(dictionary=dictionary)

    def execute(
        self,
        query: str,
        params: Optional[Tuple[Any, ...]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Execute a query and return all rows as a list of dicts.
        Always closes its own cursor, even on failure.
        """

        cursor = self.cursor()

        try:
            # MySQL enforces this server-side for the following SELECT. It is
            # intentionally best-effort for older engines that lack the variable.
            if settings.DB_QUERY_TIMEOUT_MS > 0:
                try:
                    cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (settings.DB_QUERY_TIMEOUT_MS,))
                except Error:
                    logger.debug("Server does not support MAX_EXECUTION_TIME")
            cursor.execute(query, params or ())
            return cursor.fetchall()

        except Error as e:
            # SQL can contain operational identifiers or literal values.
            logger.error("Query failed (%s); SQL text omitted from logs", type(e).__name__)
            raise Exception(f"Query Execution Error: {e}") from e

        finally:
            cursor.close()

    def explain(self, query: str) -> List[Dict[str, Any]]:
        """Return optimizer estimates using the same bounded DB connection."""
        return self.execute("EXPLAIN " + query)

    def close(self) -> None:
        """
        Close the connection if it's open. Safe to call multiple times.
        """

        if self.connection and self.connection.is_connected():
            self.connection.close()  # returns the connection to mysql-connector's pool
            logger.debug("Database connection returned to pool")


if __name__ == "__main__":
    # Quick manual connectivity test:
    #   python -m core.database
    db = Database()
    db.connect()
    print("Everything Working!")
    db.close()
