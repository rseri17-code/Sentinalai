"""Database connection management for SentinalAI.

Provides a connection-pooled SQLAlchemy engine with configurable pool
size, timeouts, and connection recycling. Falls back gracefully when
the database is unavailable (agent can still operate without persistence).

Configuration via environment variables:
    DATABASE_URL            - PostgreSQL connection string
    DATABASE_POOL_SIZE      - Core pool connections (default: 5)
    DATABASE_POOL_OVERFLOW  - Max overflow connections (default: 5)
    DATABASE_POOL_TIMEOUT   - Seconds to wait for connection (default: 30)
    DATABASE_POOL_RECYCLE   - Recycle connections after N seconds (default: 1800)
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("sentinalai.db")

_engine = None


def get_database_url() -> str:
    """Return the database URL from environment (no hardcoded fallback)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        logger.debug("DATABASE_URL not set; database features disabled")
    return url


def get_engine():
    """Return a connection-pooled SQLAlchemy engine (lazy singleton).

    Returns None if DATABASE_URL is not set or if sqlalchemy is unavailable.
    """
    global _engine
    if _engine is not None:
        return _engine

    url = get_database_url()
    if not url:
        return None

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.pool import QueuePool

        _engine = create_engine(
            url,
            poolclass=QueuePool,
            pool_size=int(os.environ.get("DATABASE_POOL_SIZE", "5")),
            max_overflow=int(os.environ.get("DATABASE_POOL_OVERFLOW", "5")),
            pool_timeout=int(os.environ.get("DATABASE_POOL_TIMEOUT", "30")),
            pool_recycle=int(os.environ.get("DATABASE_POOL_RECYCLE", "1800")),
            pool_pre_ping=True,
            echo=os.environ.get("DATABASE_ECHO", "").lower() == "true",
        )
        logger.info("Database engine created (pool_size=%s)", _engine.pool.size())
        return _engine

    except ImportError:
        logger.debug("sqlalchemy not installed; database features disabled")
        return None
    except Exception as e:
        logger.warning("Failed to create database engine: %s", e)
        return None


@contextmanager
def get_connection() -> Generator:
    """Context manager that yields a raw DBAPI connection from the pool.

    Usage:
        with get_connection() as conn:
            conn.execute("SELECT 1")
    """
    engine = get_engine()
    if engine is None:
        raise RuntimeError("Database not configured (set DATABASE_URL)")

    conn = engine.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def check_health() -> dict:
    """Check database connectivity. Returns status dict for health endpoint."""
    engine = get_engine()
    if engine is None:
        return {"database": "not_configured"}
    try:
        stmt: Any
        try:
            from sqlalchemy import text
            stmt = text("SELECT 1")
        except ImportError:
            stmt = "SELECT 1"
        with engine.connect() as conn:
            conn.execute(stmt)
        return {"database": "healthy"}
    except Exception as e:
        return {"database": "unhealthy", "error": str(e)}


def dispose():
    """Dispose the connection pool (for graceful shutdown)."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
        logger.info("Database connection pool disposed")
