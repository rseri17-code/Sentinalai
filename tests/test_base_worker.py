"""
Unit tests for the BaseWorker class.

Tests the production-grade infrastructure: dispatch, error handling,
logging, timing metrics, and WorkerError propagation.
"""

import logging
import pytest

from workers.base_worker import BaseWorker, WorkerError


class ConcreteWorker(BaseWorker):
    """Minimal concrete worker for testing."""

    worker_name = "test_worker"

    def __init__(self):
        super().__init__()
        self.register("succeed", self._succeed)
        self.register("fail", self._fail)
        self.register("return_data", self._return_data)

    def _succeed(self, params: dict) -> dict:
        return {"status": "ok"}

    def _fail(self, params: dict) -> dict:
        raise ValueError("something broke")

    def _return_data(self, params: dict) -> dict:
        return {"echo": params.get("input", "none")}


# =========================================================================
# Dispatch
# =========================================================================

class TestBaseWorkerDispatch:
    """Core action dispatch tests."""

    def setup_method(self):
        self.worker = ConcreteWorker()

    def test_registered_action_returns_result(self):
        result = self.worker.execute("succeed")
        assert result == {"status": "ok"}

    def test_unknown_action_returns_empty_dict(self):
        result = self.worker.execute("nonexistent")
        assert result == {}

    def test_params_passed_through(self):
        result = self.worker.execute("return_data", {"input": "hello"})
        assert result == {"echo": "hello"}

    def test_none_params_default_to_empty_dict(self):
        result = self.worker.execute("return_data", None)
        assert result == {"echo": "none"}

    def test_no_params_argument(self):
        result = self.worker.execute("return_data")
        assert result == {"echo": "none"}


# =========================================================================
# Error handling
# =========================================================================

class TestBaseWorkerErrorHandling:
    """Errors in handlers must be caught and logged, not propagated."""

    def setup_method(self):
        self.worker = ConcreteWorker()

    def test_handler_exception_returns_error_dict(self):
        """A failing handler should return error context instead of raising."""
        result = self.worker.execute("fail")
        assert result["error"] == "something broke"
        assert result["worker"] == "test_worker"
        assert result["action"] == "fail"

    def test_handler_exception_does_not_propagate(self):
        """execute() must never raise even if the handler does."""
        # Should not raise
        self.worker.execute("fail")

    def test_success_after_failure(self):
        """Worker must remain functional after a failed call."""
        self.worker.execute("fail")
        result = self.worker.execute("succeed")
        assert result == {"status": "ok"}


# =========================================================================
# Logging
# =========================================================================

class TestBaseWorkerLogging:
    """Verify structured logging on success and failure."""

    def setup_method(self):
        self.worker = ConcreteWorker()

    def test_success_logs_info(self, caplog):
        with caplog.at_level(logging.INFO, logger="sentinalai.worker"):
            self.worker.execute("succeed")

        assert len(caplog.records) >= 1
        record = caplog.records[-1]
        assert record.levelname == "INFO"
        assert record.worker == "test_worker"
        assert record.action == "succeed"
        assert record.success is True
        assert hasattr(record, "elapsed_ms")

    def test_failure_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="sentinalai.worker"):
            self.worker.execute("fail")

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) >= 1
        record = error_records[-1]
        assert record.worker == "test_worker"
        assert record.action == "fail"
        assert "something broke" in record.error

    def test_unknown_action_logs_debug(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="sentinalai.worker"):
            self.worker.execute("nonexistent")

        debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
        assert len(debug_records) >= 1
        assert debug_records[-1].action == "nonexistent"


# =========================================================================
# Timing
# =========================================================================

class TestBaseWorkerTiming:
    """Verify elapsed_ms is tracked."""

    def setup_method(self):
        self.worker = ConcreteWorker()

    def test_elapsed_ms_is_positive(self, caplog):
        with caplog.at_level(logging.INFO, logger="sentinalai.worker"):
            self.worker.execute("succeed")

        record = caplog.records[-1]
        assert record.elapsed_ms >= 0

    def test_elapsed_ms_is_numeric(self, caplog):
        with caplog.at_level(logging.INFO, logger="sentinalai.worker"):
            self.worker.execute("succeed")

        record = caplog.records[-1]
        assert isinstance(record.elapsed_ms, (int, float))


# =========================================================================
# worker_name attribute
# =========================================================================

class TestWorkerNameAttribute:
    """Every worker must set a meaningful worker_name."""

    def test_base_worker_has_default_name(self):
        worker = BaseWorker()
        assert worker.worker_name == "base"

    def test_concrete_worker_has_custom_name(self):
        worker = ConcreteWorker()
        assert worker.worker_name == "test_worker"

    def test_worker_name_appears_in_logs(self, caplog):
        worker = ConcreteWorker()
        worker.register("ping", lambda p: {"pong": True})
        with caplog.at_level(logging.INFO, logger="sentinalai.worker"):
            worker.execute("ping")

        record = caplog.records[-1]
        assert record.worker == "test_worker"


# =========================================================================
# WorkerError
# =========================================================================

class TestWorkerError:
    """WorkerError exception class tests."""

    def test_worker_error_attributes(self):
        cause = RuntimeError("db gone")
        err = WorkerError("ops_worker", "get_incident", cause)
        assert err.worker == "ops_worker"
        assert err.action == "get_incident"
        assert err.cause is cause

    def test_worker_error_str(self):
        cause = RuntimeError("connection lost")
        err = WorkerError("log_worker", "search_logs", cause)
        assert "log_worker" in str(err)
        assert "search_logs" in str(err)
        assert "connection lost" in str(err)

    def test_worker_error_is_exception(self):
        err = WorkerError("w", "a", RuntimeError("x"))
        assert isinstance(err, Exception)


# =========================================================================
# Registration
# =========================================================================

class TestHandlerRegistration:
    """Dynamic handler registration."""

    def test_register_new_action(self):
        worker = BaseWorker()
        worker.register("custom", lambda p: {"custom": True})
        result = worker.execute("custom")
        assert result == {"custom": True}

    def test_register_overwrites_existing(self):
        worker = BaseWorker()
        worker.register("action", lambda p: {"v": 1})
        worker.register("action", lambda p: {"v": 2})
        result = worker.execute("action")
        assert result == {"v": 2}

    def test_multiple_registrations(self):
        worker = BaseWorker()
        worker.register("a", lambda p: {"a": 1})
        worker.register("b", lambda p: {"b": 2})
        worker.register("c", lambda p: {"c": 3})
        assert worker.execute("a") == {"a": 1}
        assert worker.execute("b") == {"b": 2}
        assert worker.execute("c") == {"c": 3}


# =========================================================================
# Concrete worker instantiation
# =========================================================================

class TestConcreteWorkers:
    """All production worker classes must instantiate and have correct worker_name."""

    def test_ops_worker(self):
        from workers.ops_worker import OpsWorker
        w = OpsWorker()
        assert w.worker_name == "ops_worker"
        assert "get_incident_by_id" in w._handlers

    def test_log_worker(self):
        from workers.log_worker import LogWorker
        w = LogWorker()
        assert w.worker_name == "log_worker"
        assert "search_logs" in w._handlers
        assert "get_change_data" in w._handlers

    def test_metrics_worker(self):
        from workers.metrics_worker import MetricsWorker
        w = MetricsWorker()
        assert "query_metrics" in w._handlers
        assert "get_events" in w._handlers

    def test_apm_worker(self):
        from workers.apm_worker import ApmWorker
        w = ApmWorker()
        assert "get_golden_signals" in w._handlers

    def test_knowledge_worker(self):
        from workers.knowledge_worker import KnowledgeWorker
        w = KnowledgeWorker()
        assert "search_similar" in w._handlers


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
