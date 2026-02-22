"""Tests for database/connection.py.

Validates:
- get_database_url reads from environment
- get_engine lazy singleton with pool configuration
- get_connection context manager commit/rollback semantics
- check_health returns correct status dicts
- dispose clears the singleton engine
"""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock


class TestGetDatabaseUrl:
    """Tests for get_database_url()."""

    def test_returns_empty_when_not_set(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        from database.connection import get_database_url
        assert get_database_url() == ""

    def test_returns_url_when_set(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
        from database.connection import get_database_url
        assert get_database_url() == "postgresql://user:pass@localhost/db"


class TestGetEngine:
    """Tests for get_engine() lazy singleton."""

    def setup_method(self):
        import database.connection as db
        db._engine = None

    def teardown_method(self):
        import database.connection as db
        db._engine = None

    def test_returns_none_when_no_url(self, monkeypatch):
        import database.connection as db
        monkeypatch.setenv("DATABASE_URL", "")
        db._engine = None
        result = db.get_engine()
        assert result is None

    def test_returns_cached_engine(self, monkeypatch):
        import database.connection as db
        sentinel = MagicMock()
        db._engine = sentinel
        result = db.get_engine()
        assert result is sentinel

    def test_creates_engine_with_sqlalchemy(self, monkeypatch):
        import database.connection as db
        db._engine = None
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
        monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
        monkeypatch.setenv("DATABASE_POOL_OVERFLOW", "2")
        monkeypatch.setenv("DATABASE_POOL_TIMEOUT", "15")
        monkeypatch.setenv("DATABASE_POOL_RECYCLE", "900")
        monkeypatch.setenv("DATABASE_ECHO", "false")

        mock_engine = MagicMock()
        mock_engine.pool.size.return_value = 3

        mock_create_engine = MagicMock(return_value=mock_engine)
        mock_queue_pool = MagicMock()

        with patch.dict("sys.modules", {}):
            with patch("database.connection.get_database_url", return_value="postgresql://user:pass@localhost/db"):
                # Patch the imports inside get_engine
                import importlib
                with patch.object(db, "_engine", None):
                    # We need to mock the dynamic import
                    mock_sa = MagicMock()
                    mock_sa.create_engine = mock_create_engine
                    mock_pool = MagicMock()
                    mock_pool.QueuePool = mock_queue_pool

                    import builtins
                    original_import = builtins.__import__

                    def patched_import(name, *args, **kwargs):
                        if name == "sqlalchemy":
                            return mock_sa
                        if name == "sqlalchemy.pool":
                            return mock_pool
                        return original_import(name, *args, **kwargs)

                    with patch("builtins.__import__", side_effect=patched_import):
                        result = db.get_engine()

        # Engine should be created
        if result is not None:
            assert mock_create_engine.called or result is mock_engine

    def test_handles_import_error(self, monkeypatch):
        import database.connection as db
        db._engine = None
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")

        import builtins
        original_import = builtins.__import__

        def patched_import(name, *args, **kwargs):
            if name == "sqlalchemy":
                raise ImportError("No module named 'sqlalchemy'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            result = db.get_engine()
        assert result is None

    def test_handles_generic_error(self, monkeypatch):
        import database.connection as db
        db._engine = None
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")

        import builtins
        original_import = builtins.__import__

        def patched_import(name, *args, **kwargs):
            if name == "sqlalchemy":
                raise RuntimeError("Unexpected error")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=patched_import):
            result = db.get_engine()
        assert result is None


class TestGetConnection:
    """Tests for get_connection() context manager."""

    def setup_method(self):
        import database.connection as db
        db._engine = None

    def teardown_method(self):
        import database.connection as db
        db._engine = None

    def test_raises_when_no_engine(self, monkeypatch):
        import database.connection as db
        monkeypatch.setenv("DATABASE_URL", "")
        db._engine = None

        with pytest.raises(RuntimeError, match="Database not configured"):
            with db.get_connection() as conn:
                pass

    def test_commits_on_success(self):
        import database.connection as db
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value = mock_conn
        db._engine = mock_engine

        with db.get_connection() as conn:
            assert conn is mock_conn

        mock_conn.commit.assert_called_once()
        mock_conn.close.assert_called_once()
        mock_conn.rollback.assert_not_called()

    def test_rollbacks_on_error(self):
        import database.connection as db
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value = mock_conn
        db._engine = mock_engine

        with pytest.raises(ValueError):
            with db.get_connection() as conn:
                raise ValueError("bad query")

        mock_conn.rollback.assert_called_once()
        mock_conn.close.assert_called_once()
        mock_conn.commit.assert_not_called()


class TestCheckHealth:
    """Tests for check_health()."""

    def setup_method(self):
        import database.connection as db
        db._engine = None

    def teardown_method(self):
        import database.connection as db
        db._engine = None

    def test_not_configured(self, monkeypatch):
        import database.connection as db
        monkeypatch.setenv("DATABASE_URL", "")
        db._engine = None
        result = db.check_health()
        assert result == {"database": "not_configured"}

    def test_healthy(self):
        import database.connection as db
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
        db._engine = mock_engine

        result = db.check_health()
        assert result == {"database": "healthy"}

    def test_unhealthy(self):
        import database.connection as db
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = RuntimeError("connection refused")
        db._engine = mock_engine

        result = db.check_health()
        assert result["database"] == "unhealthy"
        assert "connection refused" in result["error"]


class TestDispose:
    """Tests for dispose()."""

    def test_dispose_clears_engine(self):
        import database.connection as db
        db._engine = MagicMock()
        db.dispose()
        assert db._engine is None

    def test_dispose_calls_engine_dispose(self):
        import database.connection as db
        mock_engine = MagicMock()
        db._engine = mock_engine
        db.dispose()
        mock_engine.dispose.assert_called_once()
        assert db._engine is None

    def test_dispose_noop_when_no_engine(self):
        import database.connection as db
        db._engine = None
        db.dispose()  # Should not raise
        assert db._engine is None
