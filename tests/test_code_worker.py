"""Tests for CodeWorker — AI diff analysis and fix generation."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from workers.code_worker import CodeWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
--- a/src/payments/processor.py
+++ b/src/payments/processor.py
@@ -40,7 +40,6 @@ class PaymentProcessor:
     def process(self, payment_id: str) -> dict:
-        if payment_id is None:
-            raise ValueError("payment_id required")
         conn = self._pool.get_connection()
         return conn.execute(f"SELECT * FROM payments WHERE id={payment_id}")
"""

SAMPLE_ERROR_CONTEXT = """\
[ERROR] NullPointerException at PaymentProcessor.process():42
[ERROR] TypeError: None is not a valid payment_id
STACK: File "src/payments/processor.py", line 42, in process
    conn = self._pool.get_connection()
NullPointerException: connection is None
"""


def _worker_with_stub_gateway():
    """CodeWorker using the default stub gateway."""
    return CodeWorker()


# ---------------------------------------------------------------------------
# analyze_diff — general
# ---------------------------------------------------------------------------

class TestAnalyzeDiff:
    def test_returns_error_when_no_diff(self):
        worker = _worker_with_stub_gateway()
        result = worker.execute("analyze_diff", {
            "repo": "myorg/service",
            "sha": "abc123",
            "diff": "",
        })
        assert "error" in result
        assert result.get("confidence", 0) == 0

    def test_returns_confidence_field(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "repo": "myorg/service",
                "sha": "abc123",
                "diff": SAMPLE_DIFF,
                "error_context": SAMPLE_ERROR_CONTEXT,
            })
        assert "confidence" in result
        assert isinstance(result["confidence"], int)

    def test_pattern_analysis_extracts_filename(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "repo": "myorg/service",
                "sha": "abc123",
                "diff": SAMPLE_DIFF,
                "error_context": "",
            })
        assert result.get("culprit_file") == "src/payments/processor.py"

    def test_pattern_analysis_source_is_pattern_match(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "diff": SAMPLE_DIFF,
            })
        assert result.get("analysis_source") == "pattern_match"

    def test_pattern_analysis_extracts_line_number_from_error(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "diff": SAMPLE_DIFF,
                "error_context": "NullPointerException at line:42)",
            })
        assert result.get("culprit_line") == 42

    def test_handles_empty_diff_gracefully(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {"diff": ""})
        assert "error" in result

    def test_llm_analysis_falls_back_on_parse_error(self):
        """If LLM returns non-JSON, fall back to pattern_match."""
        with (
            patch("supervisor.llm.is_enabled", return_value=True),
            patch("supervisor.llm.converse", return_value={"text": "This is not JSON"}),
        ):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "diff": SAMPLE_DIFF,
                "error_context": SAMPLE_ERROR_CONTEXT,
            })
        assert result.get("analysis_source") == "pattern_match"

    def test_llm_analysis_uses_result_when_valid(self):
        """If LLM returns valid JSON, use it."""
        llm_output = {
            "culprit_file": "src/payments/processor.py",
            "culprit_line": 42,
            "culprit_snippet": "conn = self._pool.get_connection()",
            "reasoning": "Removed null check allows None payment_id",
            "confidence": 92,
        }
        import json
        with (
            patch("supervisor.llm.is_enabled", return_value=True),
            patch("supervisor.llm.converse", return_value={"text": json.dumps(llm_output)}),
        ):
            worker = _worker_with_stub_gateway()
            result = worker.execute("analyze_diff", {
                "diff": SAMPLE_DIFF,
                "error_context": SAMPLE_ERROR_CONTEXT,
            })
        assert result["confidence"] == 92
        assert result["culprit_file"] == "src/payments/processor.py"
        assert result["analysis_source"] == "llm"


# ---------------------------------------------------------------------------
# generate_fix
# ---------------------------------------------------------------------------

class TestGenerateFix:
    def test_returns_none_fix_when_confidence_too_low(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {"confidence": 30, "culprit_file": "foo.py"},
                "service": "payment-service",
            })
        assert result["fix_type"] == "none"
        assert "confidence" in result["fix_description"].lower() or "insufficient" in result["fix_description"].lower()

    def test_returns_rollback_when_no_llm(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {"confidence": 80, "culprit_file": "src/foo.py"},
                "service": "payment-service",
                "sha": "abc123",
            })
        assert result["fix_type"] == "rollback"
        assert result["requires_approval"] is True

    def test_rollback_includes_kubectl_command(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {"confidence": 75, "culprit_file": "src/foo.py"},
                "service": "payment-service",
                "sha": "abc123",
            })
        imm = result.get("immediate_action", {})
        assert "kubectl" in imm.get("command", "").lower()
        assert "payment-service" in imm.get("command", "")

    def test_fix_includes_incident_id(self):
        with patch("supervisor.llm.is_enabled", return_value=False):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {"confidence": 80, "culprit_file": "src/foo.py"},
                "service": "svc",
                "incident_id": "INC0012345",
            })
        assert result.get("incident_id") == "INC0012345"

    def test_llm_fix_generation_with_valid_response(self):
        llm_fix = {
            "fix_type": "code_fix",
            "fix_description": "Restore null check for payment_id",
            "pr_title": "fix: restore null check in PaymentProcessor",
            "pr_body": "## Root Cause\nRemoved null check caused NPE",
            "patch": "--- a/processor.py\n+++ b/processor.py\n@@ -40 +40 @@\n+        if payment_id is None:\n+            raise ValueError()",
            "confidence": 88,
            "risk_level": "low",
        }
        import json
        with (
            patch("supervisor.llm.is_enabled", return_value=True),
            patch("supervisor.llm.converse", return_value={"text": json.dumps(llm_fix)}),
        ):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {
                    "confidence": 88,
                    "culprit_file": "src/payments/processor.py",
                    "culprit_line": 42,
                    "reasoning": "Removed null check",
                },
                "service": "payment-service",
                "repo": "myorg/payment-service",
                "sha": "abc123",
                "diff": SAMPLE_DIFF,
                "error_context": SAMPLE_ERROR_CONTEXT,
            })
        assert result["fix_type"] == "code_fix"
        assert result["pr_title"] == "fix: restore null check in PaymentProcessor"
        assert result["confidence"] == 88
        assert result["requires_approval"] is True

    def test_llm_fix_falls_back_to_rollback_on_error(self):
        with (
            patch("supervisor.llm.is_enabled", return_value=True),
            patch("supervisor.llm.converse", side_effect=RuntimeError("rate limit")),
        ):
            worker = _worker_with_stub_gateway()
            result = worker.execute("generate_fix", {
                "analysis": {"confidence": 80, "culprit_file": "src/foo.py"},
                "service": "my-service",
                "sha": "abc123",
            })
        assert result["fix_type"] == "rollback"


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------

class TestCodeWorkerRegistration:
    def test_analyze_diff_is_registered(self):
        worker = CodeWorker()
        assert "analyze_diff" in worker._handlers

    def test_generate_fix_is_registered(self):
        worker = CodeWorker()
        assert "generate_fix" in worker._handlers

    def test_execute_routes_to_correct_action(self):
        worker = CodeWorker()
        with patch("supervisor.llm.is_enabled", return_value=False):
            result = worker.execute("analyze_diff", {"diff": SAMPLE_DIFF})
        assert "confidence" in result
