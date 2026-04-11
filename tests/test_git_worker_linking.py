"""Tests for git_worker incident-linking actions (annotate_commit_with_incident,
search_commits_by_incident) and the expanded stub_mcp_server git stubs."""
from __future__ import annotations

import os
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("INCIDENT_GIT_LINKER_ENABLED", "true")

from workers.git_worker import GitWorker


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gateway():
    gw = MagicMock()
    gw.invoke.return_value = {}
    return gw


@pytest.fixture
def worker(mock_gateway):
    return GitWorker(gateway=mock_gateway)


# ---------------------------------------------------------------------------
# annotate_commit_with_incident
# ---------------------------------------------------------------------------

class TestAnnotateCommitWithIncident:

    def test_requires_incident_id(self, worker):
        result = worker.execute("annotate_commit_with_incident", {
            "commit_sha": "abc123", "repo": "org/svc",
        })
        assert "error" in result

    def test_requires_commit_sha(self, worker):
        result = worker.execute("annotate_commit_with_incident", {
            "incident_id": "INC-001", "repo": "org/svc",
        })
        assert "error" in result

    def test_requires_repo(self, worker):
        result = worker.execute("annotate_commit_with_incident", {
            "incident_id": "INC-001", "commit_sha": "abc123",
        })
        assert "error" in result

    def test_links_commit_to_incident(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "gw_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("annotate_commit_with_incident", {
            "incident_id": "INC-555",
            "commit_sha": "deadbeef12345678",
            "repo": "org/payment-service",
            "relationship": "caused_by",
            "confidence": 0.9,
            "author": "alice",
            "commit_message": "fix: reduce pool size",
        })
        assert result.get("linked") is True
        assert result["link"]["incident_id"] == "INC-555"
        assert result["link"]["commit_sha"] == "deadbeef12345678"
        assert result["link"]["relationship"] == "caused_by"

    def test_accepts_sha_alias(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "alias_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("annotate_commit_with_incident", {
            "incident_id": "INC-556",
            "sha": "cafebabe12345678",  # "sha" alias instead of "commit_sha"
            "repo": "org/svc",
        })
        assert result.get("linked") is True

    def test_default_relationship_is_caused_by(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "def_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("annotate_commit_with_incident", {
            "incident_id": "INC-557",
            "commit_sha": "aabb1122",
            "repo": "org/svc",
        })
        assert result["link"]["relationship"] == "caused_by"

    def test_disabled_linker_returns_not_linked(self, monkeypatch):
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", False)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("annotate_commit_with_incident", {
            "incident_id": "INC-999",
            "commit_sha": "abc123",
            "repo": "org/svc",
        })
        assert result.get("linked") is False
        assert result.get("reason") == "linker_disabled"


# ---------------------------------------------------------------------------
# search_commits_by_incident
# ---------------------------------------------------------------------------

class TestSearchCommitsByIncident:

    def test_requires_incident_id(self, worker):
        result = worker.execute("search_commits_by_incident", {})
        assert "error" in result

    def test_returns_empty_list_for_unknown_incident(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "empty_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("search_commits_by_incident", {"incident_id": "INC-NOTEXIST"})
        assert result["commits"] == []
        assert result["total"] == 0

    def test_returns_linked_commits(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "search_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        from supervisor.incident_git_linker import link_incident_to_commit
        link_incident_to_commit("INC-700", "sha-cause", "org/svc", "caused_by", save=False)
        link_incident_to_commit("INC-700", "sha-fix", "org/svc", "fixed_by", save=False)

        gw = MagicMock()
        w = GitWorker(gateway=gw)
        result = w.execute("search_commits_by_incident", {"incident_id": "INC-700"})
        assert result["total"] == 2

    def test_filter_by_relationship(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "filter_idx.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        from supervisor.incident_git_linker import link_incident_to_commit
        link_incident_to_commit("INC-800", "sha-cause", "org/svc", "caused_by", save=False)
        link_incident_to_commit("INC-800", "sha-fix", "org/svc", "fixed_by", save=False)

        gw = MagicMock()
        w = GitWorker(gateway=gw)

        caused = w.execute("search_commits_by_incident", {
            "incident_id": "INC-800",
            "relationship": "caused_by",
        })
        assert caused["total"] == 1
        assert caused["commits"][0]["relationship"] == "caused_by"


# ---------------------------------------------------------------------------
# Stub MCP Server — new git + visual stubs
# ---------------------------------------------------------------------------

class TestStubMcpServerNewEndpoints:
    """Verify the stub handlers return valid responses for the new actions."""

    @pytest.fixture(autouse=True)
    def _import_handlers(self):
        from scripts.stub_mcp_server import _github, _sysdig, _dynatrace
        self._github = _github
        self._sysdig = _sysdig
        self._dynatrace = _dynatrace

    # GitHub git stubs
    def test_git_log(self):
        result = self._github("git_log", {"repo": "org/svc"})
        assert "commits" in result
        assert len(result["commits"]) >= 1
        commit = result["commits"][0]
        assert "sha" in commit
        assert "message" in commit
        assert "files_changed" in commit

    def test_git_blame(self):
        result = self._github("git_blame", {"repo": "org/svc", "path": "src/config.py", "line_start": 42})
        assert "blame" in result
        assert result["blame"][0]["line"] == 42

    def test_git_show(self):
        result = self._github("git_show", {"repo": "org/svc", "sha": "abc123"})
        assert result["sha"] == "abc123"
        assert "patch" in result
        assert "parent_sha" in result

    def test_git_diff(self):
        result = self._github("git_diff", {"repo": "org/svc", "base_sha": "abc", "head_sha": "def"})
        assert "diff" in result
        assert "files_changed" in result

    def test_get_pr_for_commit(self):
        result = self._github("get_pr_for_commit", {"repo": "org/svc", "sha": "abc123"})
        assert "pr_number" in result
        assert "pr_title" in result

    # Sysdig visual stubs
    def test_get_metric_chart(self):
        result = self._sysdig("get_metric_chart", {
            "service": "payment-service",
            "metric": "net.http.request.time",
        })
        assert "chart" in result
        chart = result["chart"]
        assert "url" in chart
        assert "annotation" in chart
        assert chart["source"] == "sysdig"

    def test_get_dashboard_snapshot(self):
        result = self._sysdig("get_dashboard_snapshot", {"service": "payment-service"})
        assert "dashboard" in result
        dash = result["dashboard"]
        assert "url" in dash
        assert "charts" in dash
        assert len(dash["charts"]) >= 1

    # Dynatrace topology + trace stubs
    def test_get_topology(self):
        result = self._dynatrace("get_topology", {"service": "payment-service"})
        assert "topology" in result
        topo = result["topology"]
        assert "nodes" in topo
        assert "edges" in topo
        assert "image_url" in topo

    def test_get_trace(self):
        result = self._dynatrace("get_trace", {
            "service": "payment-service",
            "trace_id": "abc123def456789012345678901234ab",
        })
        assert "spans" in result
        spans = result["spans"]
        assert len(spans) >= 3
        # There should be at least one error span
        error_spans = [s for s in spans if s.get("error")]
        assert len(error_spans) >= 1

    def test_error_samples_includes_trace_id(self):
        result = self._dynatrace("get_error_samples", {"service": "payment-service"})
        errors = result.get("errors", [])
        assert len(errors) >= 1
        assert "trace_id" in errors[0]
