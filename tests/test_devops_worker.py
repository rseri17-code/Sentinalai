"""
Test suite for the DevOps (GitHub) worker.
Validates determinism, correct dispatching, parameter validation, and error handling.
"""

import pytest

from workers.devops_worker import DevopsWorker


class TestDevopsWorkerInit:
    """DevopsWorker instantiation and registration."""

    def test_worker_name(self):
        worker = DevopsWorker()
        assert worker.worker_name == "devops_worker"

    def test_registers_all_actions(self):
        worker = DevopsWorker()
        for action in ("get_recent_deployments", "get_pr_details", "get_commit_diff", "get_workflow_runs"):
            result = worker.execute(action, {"service": "test-svc", "repo": "org/repo", "pr_number": 1, "sha": "abc123"})
            assert isinstance(result, dict)


class TestGetRecentDeployments:
    """Tests for GitHub recent deployments (merged PRs, releases)."""

    def setup_method(self):
        self.worker = DevopsWorker()

    def test_returns_dict(self):
        result = self.worker.execute("get_recent_deployments", {"service": "payment-service"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        r1 = self.worker.execute("get_recent_deployments", {"service": "payment-service"})
        r2 = self.worker.execute("get_recent_deployments", {"service": "payment-service"})
        assert r1 == r2

    def test_accepts_repo_param(self):
        result = self.worker.execute("get_recent_deployments", {"repo": "org/payment-service"})
        assert isinstance(result, dict)
        assert "error" not in result or result["error"] != "service or repo required"

    def test_missing_both_returns_error(self):
        result = self.worker.execute("get_recent_deployments", {})
        assert result.get("error") == "service or repo required"


class TestGetPrDetails:
    """Tests for GitHub PR details retrieval."""

    def setup_method(self):
        self.worker = DevopsWorker()

    def test_returns_dict(self):
        result = self.worker.execute(
            "get_pr_details",
            {"repo": "org/payment-service", "pr_number": 42},
        )
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"repo": "org/payment-service", "pr_number": 42}
        r1 = self.worker.execute("get_pr_details", params)
        r2 = self.worker.execute("get_pr_details", params)
        assert r1 == r2

    def test_missing_repo_returns_error(self):
        result = self.worker.execute("get_pr_details", {"pr_number": 42})
        assert result.get("error") == "repo and pr_number required"

    def test_missing_pr_number_returns_error(self):
        result = self.worker.execute("get_pr_details", {"repo": "org/repo"})
        assert result.get("error") == "repo and pr_number required"


class TestGetCommitDiff:
    """Tests for GitHub commit diff retrieval."""

    def setup_method(self):
        self.worker = DevopsWorker()

    def test_returns_dict(self):
        result = self.worker.execute(
            "get_commit_diff",
            {"repo": "org/payment-service", "sha": "abc123def456"},
        )
        assert isinstance(result, dict)

    def test_deterministic(self):
        params = {"repo": "org/payment-service", "sha": "abc123def456"}
        r1 = self.worker.execute("get_commit_diff", params)
        r2 = self.worker.execute("get_commit_diff", params)
        assert r1 == r2

    def test_missing_repo_returns_error(self):
        result = self.worker.execute("get_commit_diff", {"sha": "abc123"})
        assert result.get("error") == "repo and sha required"

    def test_missing_sha_returns_error(self):
        result = self.worker.execute("get_commit_diff", {"repo": "org/repo"})
        assert result.get("error") == "repo and sha required"


class TestGetWorkflowRuns:
    """Tests for GitHub Actions workflow run status."""

    def setup_method(self):
        self.worker = DevopsWorker()

    def test_returns_dict(self):
        result = self.worker.execute("get_workflow_runs", {"service": "payment-service"})
        assert isinstance(result, dict)

    def test_deterministic(self):
        r1 = self.worker.execute("get_workflow_runs", {"service": "payment-service"})
        r2 = self.worker.execute("get_workflow_runs", {"service": "payment-service"})
        assert r1 == r2

    def test_accepts_repo_param(self):
        result = self.worker.execute("get_workflow_runs", {"repo": "org/payment-service"})
        assert isinstance(result, dict)

    def test_missing_both_returns_error(self):
        result = self.worker.execute("get_workflow_runs", {})
        assert result.get("error") == "service or repo required"


class TestDevopsWorkerUnknownAction:
    """Unknown action handling."""

    def test_unknown_action_returns_empty(self):
        worker = DevopsWorker()
        result = worker.execute("nonexistent_action", {})
        assert isinstance(result, dict)
        assert result == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
