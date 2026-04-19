"""Tests for workers.git_worker."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from workers.git_worker import (
    GitWorker,
    _pick_breaking_commit,
    _bisect_confidence,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gateway():
    gw = MagicMock()
    gw.invoke.return_value = {
        "commits": [
            {
                "sha": "abc123",
                "message": "Deploy payment-service v2.1.0 — reduce MAX_CONNECTIONS",
                "author": "devops-automation",
                "date": "2024-01-15T14:00:00Z",
                "files_changed": ["services/payment/config.yaml"],
                "insertions": 5,
                "deletions": 2,
            },
            {
                "sha": "def456",
                "message": "Fix typo in README",
                "author": "alice",
                "date": "2024-01-15T13:30:00Z",
                "files_changed": ["README.md"],
                "insertions": 1,
                "deletions": 1,
            },
        ]
    }
    return gw


@pytest.fixture
def worker(mock_gateway):
    return GitWorker(gateway=mock_gateway)


# ---------------------------------------------------------------------------
# git_log_for_service
# ---------------------------------------------------------------------------

class TestGitLogForService:

    def test_requires_repo_or_service(self, worker):
        result = worker.execute("git_log_for_service", {})
        assert "error" in result

    def test_calls_gateway_with_repo(self, worker, mock_gateway):
        worker.execute("git_log_for_service", {"repo": "org/payment-service"})
        mock_gateway.invoke.assert_called_once()
        args = mock_gateway.invoke.call_args[0]
        assert "github.git_log" in args[0]

    def test_passes_since_sha(self, worker, mock_gateway):
        worker.execute("git_log_for_service", {
            "repo": "org/svc",
            "since_sha": "abc123",
        })
        _, kwargs = mock_gateway.invoke.call_args
        call_params = mock_gateway.invoke.call_args[0][2]
        assert call_params["since_sha"] == "abc123"

    def test_default_max_commits(self, worker, mock_gateway):
        worker.execute("git_log_for_service", {"repo": "org/svc"})
        call_params = mock_gateway.invoke.call_args[0][2]
        assert call_params["max_commits"] == 20


# ---------------------------------------------------------------------------
# git_blame_line
# ---------------------------------------------------------------------------

class TestGitBlameLine:

    def test_requires_repo(self, worker):
        result = worker.execute("git_blame_line", {"path": "file.py", "line_start": 10})
        assert "error" in result

    def test_requires_path(self, worker):
        result = worker.execute("git_blame_line", {"repo": "org/svc", "line_start": 10})
        assert "error" in result

    def test_requires_line_start(self, worker):
        result = worker.execute("git_blame_line", {"repo": "org/svc", "path": "file.py"})
        assert "error" in result

    def test_calls_gateway(self, worker, mock_gateway):
        worker.execute("git_blame_line", {
            "repo": "org/svc", "path": "file.py", "line_start": 42,
        })
        mock_gateway.invoke.assert_called_once()
        args = mock_gateway.invoke.call_args[0]
        assert "github.git_blame" in args[0]

    def test_line_end_defaults_to_line_start(self, worker, mock_gateway):
        worker.execute("git_blame_line", {
            "repo": "org/svc", "path": "file.py", "line_start": 42,
        })
        call_params = mock_gateway.invoke.call_args[0][2]
        assert call_params["line_end"] == 42


# ---------------------------------------------------------------------------
# git_find_breaking_change
# ---------------------------------------------------------------------------

class TestGitFindBreakingChange:

    def test_requires_repo_or_service(self, worker):
        result = worker.execute("git_find_breaking_change", {})
        assert "error" in result

    def test_returns_breaking_commit(self, worker, mock_gateway):
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/payment-service",
            "incident_time": "2024-01-15T14:05:00Z",
        })
        assert "breaking_commit" in result
        assert result["breaking_commit"] is not None

    def test_returns_candidate_window(self, worker, mock_gateway):
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/payment-service",
        })
        assert "candidate_window" in result
        assert isinstance(result["candidate_window"], list)

    def test_bisect_strategy_time_anchored(self, worker, mock_gateway):
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
            "incident_time": "2024-01-15T14:00:00Z",
        })
        assert result["bisect_strategy"] == "time_anchored"

    def test_bisect_strategy_full_no_time(self, worker, mock_gateway):
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
        })
        assert result["bisect_strategy"] == "full"

    def test_bisect_strategy_path_filtered(self, worker, mock_gateway):
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
            "paths": ["services/payment/"],
        })
        assert result["bisect_strategy"] == "path_filtered"

    def test_max_depth_capped(self, worker, mock_gateway):
        worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
            "max_depth": 999,  # should be capped at GIT_BISECT_MAX_COMMITS
        })
        # Find the git_log call (first call to invoke)
        from workers.git_worker import GIT_BISECT_MAX_COMMITS
        first_call_params = mock_gateway.invoke.call_args_list[0][0][2]
        assert first_call_params["max_commits"] <= GIT_BISECT_MAX_COMMITS

    def test_empty_commits_returns_null_breaking(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {"commits": []}
        result = worker.execute("git_find_breaking_change", {"repo": "org/svc"})
        assert result["breaking_commit"] is None
        assert result["confidence"] == 0.0

    def test_gateway_error_propagated(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {"error": "rate limited"}
        result = worker.execute("git_find_breaking_change", {"repo": "org/svc"})
        assert "error" in result

    def test_path_filtered_prefers_matching_file(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {
            "commits": [
                {
                    "sha": "first",
                    "message": "Unrelated README change",
                    "author": "bob",
                    "date": "2024-01-15T14:01:00Z",
                    "files_changed": ["README.md"],
                },
                {
                    "sha": "second",
                    "message": "Change config",
                    "author": "alice",
                    "date": "2024-01-15T14:00:00Z",
                    "files_changed": ["services/payment/config.yaml"],
                },
            ]
        }
        result = worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
            "paths": ["services/payment/"],
        })
        # Should prefer the commit matching the path
        assert result["breaking_commit"]["sha"] == "second"


# ---------------------------------------------------------------------------
# git_diff_range
# ---------------------------------------------------------------------------

class TestGitDiffRange:

    def test_requires_repo(self, worker):
        result = worker.execute("git_diff_range", {"base_sha": "abc123"})
        assert "error" in result

    def test_requires_base_sha(self, worker):
        result = worker.execute("git_diff_range", {"repo": "org/svc"})
        assert "error" in result

    def test_calls_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {"diff": "+line\n-old_line"}
        worker.execute("git_diff_range", {"repo": "org/svc", "base_sha": "abc123"})
        mock_gateway.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# git_show_commit
# ---------------------------------------------------------------------------

class TestGitShowCommit:

    def test_requires_repo(self, worker):
        result = worker.execute("git_show_commit", {"sha": "abc123"})
        assert "error" in result

    def test_requires_sha(self, worker):
        result = worker.execute("git_show_commit", {"repo": "org/svc"})
        assert "error" in result

    def test_calls_gateway(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {"sha": "abc123", "message": "fix: reduce pool"}
        worker.execute("git_show_commit", {"repo": "org/svc", "sha": "abc123"})
        mock_gateway.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

class TestPickBreakingCommit:

    COMMITS = [
        {"sha": "aaa", "message": "Config change", "files_changed": ["services/payment/config.yaml"]},
        {"sha": "bbb", "message": "README update", "files_changed": ["README.md"]},
    ]

    def test_prefers_path_matching_commit(self):
        commit = _pick_breaking_commit(self.COMMITS, paths=["services/payment/"])
        assert commit["sha"] == "aaa"

    def test_falls_back_to_first_when_no_path_match(self):
        commit = _pick_breaking_commit(self.COMMITS, paths=["nonexistent/"])
        assert commit["sha"] == "aaa"  # first commit

    def test_returns_none_for_empty_list(self):
        assert _pick_breaking_commit([], paths=[]) is None

    def test_no_paths_returns_first(self):
        commit = _pick_breaking_commit(self.COMMITS, paths=[])
        assert commit["sha"] == "aaa"


class TestBisectConfidence:

    def test_zero_confidence_for_none(self):
        assert _bisect_confidence(None, [], []) == 0.0

    def test_high_confidence_single_commit(self):
        commit = {"sha": "a", "files_changed": ["service/config.yaml"]}
        score = _bisect_confidence(commit, [commit], paths=["service/"])
        assert score >= 0.9

    def test_lower_confidence_many_commits(self):
        commits = [{"sha": f"s{i}", "files_changed": []} for i in range(20)]
        score = _bisect_confidence(commits[0], commits, paths=[])
        assert score < 0.7

    def test_path_match_boosts_score(self):
        commit_match = {"sha": "a", "files_changed": ["services/payment/config.yaml"]}
        commit_no_match = {"sha": "b", "files_changed": ["README.md"]}
        score_match = _bisect_confidence(commit_match, [commit_match], paths=["services/payment/"])
        score_no_match = _bisect_confidence(commit_no_match, [commit_no_match], paths=["services/payment/"])
        assert score_match > score_no_match


# ---------------------------------------------------------------------------
# Coverage gap-fill tests
# ---------------------------------------------------------------------------

class TestFindBreakingChangeGoodSha:
    """Cover line 182: good_sha branch in git_find_breaking_change."""

    def test_good_sha_added_to_log_params(self, worker, mock_gateway):
        worker.execute("git_find_breaking_change", {
            "repo": "org/svc",
            "good_sha": "baselinesha123",
        })
        # Find the git_log call
        log_call = mock_gateway.invoke.call_args_list[0]
        call_params = log_call[0][2]
        assert call_params.get("since_sha") == "baselinesha123"


class TestFindBreakingChangeNullBreaking:
    """Cover line 211: enriched = None when breaking_commit is None."""

    def test_null_breaking_commit_from_pick(self, worker, mock_gateway):
        mock_gateway.invoke.return_value = {"commits": [{"sha": "x", "files_changed": []}]}
        with patch("workers.git_worker._pick_breaking_commit", return_value=None):
            result = worker.execute("git_find_breaking_change", {"repo": "org/svc"})
        assert result["breaking_commit"] is None


class TestEnrichCommit:
    """Cover lines 339 and 349-351 in _enrich_commit."""

    def test_enrich_commit_no_sha_returns_original(self, worker):
        commit = {"message": "fix: no sha here", "author": "alice"}
        result = worker._enrich_commit("org/svc", commit)
        assert result is commit  # returned as-is when sha is empty

    def test_enrich_commit_gateway_exception_returns_original(self, worker, mock_gateway):
        mock_gateway.invoke.side_effect = RuntimeError("GitHub API error")
        commit = {"sha": "abc123def456", "message": "fix: pool", "author": "alice"}
        result = worker._enrich_commit("org/svc", commit)
        # Exception is caught and logged; original commit is returned
        assert result is commit


class TestBisectConfidenceMediumWindow:
    """Cover line 402: elif n <= 10: score += 0.10."""

    def test_medium_window_partial_bonus(self):
        # 4-10 commits: +0.10 bonus
        commits = [{"sha": f"s{i}", "files_changed": []} for i in range(5)]
        score = _bisect_confidence(commits[0], commits, paths=[])
        # baseline 0.5 + 0.10 (5 commits in window) = 0.60
        assert score == 0.60
