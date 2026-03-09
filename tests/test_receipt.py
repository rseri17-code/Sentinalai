"""Tests for evidence receipt system."""



from supervisor.receipt import (
    Receipt,
    ReceiptCollector,
    _count_results,
    _redact_params,
)


class TestReceipt:
    def test_default_fields(self):
        r = Receipt(tool="ops_worker", action="get_incident_by_id")
        assert r.tool == "ops_worker"
        assert r.action == "get_incident_by_id"
        assert r.status == "pending"
        assert len(r.correlation_id) == 12

    def test_to_dict_roundtrip(self):
        r = Receipt(tool="log_worker", action="search_logs", case_id="INC123")
        d = r.to_dict()
        r2 = Receipt.from_dict(d)
        assert r2.tool == r.tool
        assert r2.action == r.action
        assert r2.case_id == r.case_id

    def test_from_dict_ignores_extra_keys(self):
        d = {"tool": "x", "action": "y", "extra_key": "ignored"}
        r = Receipt.from_dict(d)
        assert r.tool == "x"
        assert r.action == "y"


class TestReceiptCollector:
    def test_start_and_finish_success(self):
        rc = ReceiptCollector(case_id="INC001")
        receipt = rc.start("ops_worker", "get_incident_by_id", {"incident_id": "INC001"})
        assert receipt.status == "pending"

        rc.finish(receipt, {"incident": {"id": "INC001"}})
        assert receipt.status == "success"
        assert receipt.elapsed_ms >= 0
        assert receipt.result_count == 1

    def test_start_and_finish_error(self):
        rc = ReceiptCollector(case_id="INC002")
        receipt = rc.start("log_worker", "search_logs", {"query": "timeout"})
        rc.finish(receipt, None, error="connection timeout")
        assert receipt.status == "error"
        assert receipt.error == "connection timeout"

    def test_to_list(self):
        rc = ReceiptCollector(case_id="INC003")
        r1 = rc.start("w1", "a1", {})
        rc.finish(r1, {})
        r2 = rc.start("w2", "a2", {})
        rc.finish(r2, {})

        result = rc.to_list()
        assert len(result) == 2
        assert all(isinstance(r, dict) for r in result)

    def test_summary(self):
        rc = ReceiptCollector(case_id="INC004")
        r1 = rc.start("w1", "a1", {})
        rc.finish(r1, {"results": [1, 2, 3]})
        r2 = rc.start("w2", "a2", {})
        rc.finish(r2, None, error="fail")

        s = rc.summary()
        assert s["case_id"] == "INC004"
        assert s["total_calls"] == 2
        assert s["succeeded"] == 1
        assert s["failed"] == 1


class TestCountResults:
    def test_none_input(self):
        assert _count_results(None) == 0

    def test_empty_dict(self):
        assert _count_results({}) == 0

    def test_results_list(self):
        assert _count_results({"results": [1, 2, 3]}) == 3

    def test_events_list(self):
        assert _count_results({"events": [1]}) == 1

    def test_incident_key(self):
        assert _count_results({"incident": {"id": "INC1"}}) == 1

    def test_nested_results(self):
        assert _count_results({"metrics": {"results": [1, 2]}}) == 2


class TestRedactParams:
    def test_redacts_sensitive_keys(self):
        params = {"query": "timeout", "password": "secret123", "token": "abc"}
        result = _redact_params(params)
        assert result["query"] == "timeout"
        assert result["password"] == "***REDACTED***"
        assert result["token"] == "***REDACTED***"

    def test_preserves_safe_keys(self):
        params = {"service": "web", "incident_id": "INC1"}
        result = _redact_params(params)
        assert result == params
