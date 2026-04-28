"""Tests for supervisor.incident_git_linker."""
from __future__ import annotations

import json
import os


os.environ.setdefault("INCIDENT_GIT_LINKER_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helper: isolated index (avoid global state leaking between tests)
# ---------------------------------------------------------------------------

def _make_linker(tmp_path):
    """Return a fresh _GitIncidentIndex backed by a tmp file."""
    from supervisor.incident_git_linker import _GitIncidentIndex, GitLink
    return _GitIncidentIndex(), GitLink


# ---------------------------------------------------------------------------
# GitLink dataclass
# ---------------------------------------------------------------------------

class TestGitLink:

    def test_short_sha(self):
        from supervisor.incident_git_linker import GitLink
        link = GitLink("INC-001", "abc123def456", "org/repo", "caused_by")
        assert link.short_sha == "abc123de"

    def test_to_dict_round_trip(self):
        from supervisor.incident_git_linker import GitLink
        link = GitLink(
            incident_id="INC-002",
            commit_sha="deadbeef" * 4,
            repo="org/svc",
            relationship="fixed_by",
            confidence=0.9,
            author="alice",
            commit_message="fix: pool size",
            pr_number=42,
        )
        d = link.to_dict()
        link2 = GitLink.from_dict(d)
        assert link2.incident_id == link.incident_id
        assert link2.commit_sha == link.commit_sha
        assert link2.pr_number == 42
        assert link2.confidence == 0.9


# ---------------------------------------------------------------------------
# _GitIncidentIndex
# ---------------------------------------------------------------------------

class TestGitIncidentIndex:

    def _idx(self):
        from supervisor.incident_git_linker import _GitIncidentIndex
        return _GitIncidentIndex()

    def test_link_and_forward_lookup(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        link = GitLink("INC-001", "abc123", "org/svc", "caused_by", confidence=0.9)
        idx.link(link)
        results = idx.get_commits_for_incident("INC-001")
        assert len(results) == 1
        assert results[0].commit_sha == "abc123"

    def test_reverse_lookup(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        link = GitLink("INC-002", "deadbeef12345678", "org/svc", "caused_by")
        idx.link(link)
        results = idx.get_incidents_for_commit("deadbeef12345678")
        assert len(results) == 1
        assert results[0].incident_id == "INC-002"

    def test_reverse_lookup_short_sha_prefix(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        full_sha = "deadbeef" + "0" * 24
        link = GitLink("INC-003", full_sha, "org/svc", "caused_by")
        idx.link(link)
        results = idx.get_incidents_for_commit("deadbeef")
        assert len(results) == 1

    def test_dedup_idempotent(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        link = GitLink("INC-004", "abc123", "org/svc", "caused_by", confidence=0.7)
        idx.link(link)
        idx.link(link)  # same again
        assert len(idx.get_commits_for_incident("INC-004")) == 1

    def test_dedup_upgrades_confidence(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        l1 = GitLink("INC-005", "abc123", "org/svc", "caused_by", confidence=0.6)
        l2 = GitLink("INC-005", "abc123", "org/svc", "caused_by", confidence=0.95)
        idx.link(l1)
        idx.link(l2)
        results = idx.get_commits_for_incident("INC-005")
        assert len(results) == 1
        assert results[0].confidence == 0.95

    def test_multiple_relationships_same_commit(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        idx.link(GitLink("INC-006", "sha1", "org/svc", "caused_by"))
        idx.link(GitLink("INC-006", "sha1", "org/svc", "fixed_by"))
        caused = idx.get_commits_for_incident("INC-006", "caused_by")
        fixed = idx.get_commits_for_incident("INC-006", "fixed_by")
        assert len(caused) == 1
        assert len(fixed) == 1

    def test_has_link(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        idx.link(GitLink("INC-007", "abcdef12", "org/svc", "related"))
        assert idx.has_link("INC-007", "abcdef12") is True
        assert idx.has_link("INC-007", "00000000") is False

    def test_stats(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        idx.link(GitLink("INC-008", "sha1", "org/svc", "caused_by"))
        idx.link(GitLink("INC-008", "sha2", "org/svc", "fixed_by"))
        idx.link(GitLink("INC-009", "sha3", "org/svc2", "related"))
        stats = idx.stats()
        assert stats["incidents_linked"] == 2
        assert stats["total_links"] == 3

    def test_get_incident_ids_for_repo(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        idx.link(GitLink("INC-010", "sha1", "org/repo-a", "caused_by"))
        idx.link(GitLink("INC-011", "sha2", "org/repo-a", "fixed_by"))
        idx.link(GitLink("INC-012", "sha3", "org/repo-b", "caused_by"))
        ids = idx.get_incident_ids_for_repo("org/repo-a")
        assert set(ids) == {"INC-010", "INC-011"}

    def test_sorted_by_confidence_desc(self):
        from supervisor.incident_git_linker import GitLink
        idx = self._idx()
        idx.link(GitLink("INC-013", "sha-low", "org/svc", "related", confidence=0.3))
        idx.link(GitLink("INC-013", "sha-high", "org/svc", "related", confidence=0.9))
        results = idx.get_commits_for_incident("INC-013")
        assert results[0].confidence >= results[1].confidence

    def test_persistence_round_trip(self, tmp_path):
        from supervisor.incident_git_linker import _GitIncidentIndex, GitLink
        idx = _GitIncidentIndex()
        idx.link(GitLink("INC-014", "aabbccdd", "org/svc", "caused_by", confidence=0.8))
        path = str(tmp_path / "idx.json")
        idx.save(path)
        idx2 = _GitIncidentIndex.from_dict(json.load(open(path)))
        results = idx2.get_commits_for_incident("INC-014")
        assert len(results) == 1
        assert results[0].commit_sha == "aabbccdd"


# ---------------------------------------------------------------------------
# Module-level public API
# ---------------------------------------------------------------------------

class TestPublicAPI:

    def test_link_incident_to_commit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "test_idx.json"))
        # Reset module-level singleton
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        from supervisor.incident_git_linker import link_incident_to_commit, get_commits_for_incident
        link = link_incident_to_commit(
            "INC-100", "cafebabe12345678", "org/svc", "caused_by",
            confidence=0.85, author="bob", commit_message="fix: pool",
        )
        assert link is not None
        assert link.incident_id == "INC-100"
        commits = get_commits_for_incident("INC-100")
        assert len(commits) == 1
        assert commits[0]["commit_sha"] == "cafebabe12345678"

    def test_get_incidents_for_commit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "test_idx2.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        from supervisor.incident_git_linker import link_incident_to_commit, get_incidents_for_commit
        link_incident_to_commit("INC-200", "1234abcd5678efgh", "org/svc2", "fixed_by")
        results = get_incidents_for_commit("1234abcd5678efgh")
        assert any(r["incident_id"] == "INC-200" for r in results)

    def test_get_incident_audit_trail(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_INDEX_PATH", str(tmp_path / "test_idx3.json"))
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "_index", None)

        from supervisor.incident_git_linker import link_incident_to_commit, get_incident_audit_trail
        link_incident_to_commit("INC-300", "sha-cause", "org/svc3", "caused_by")
        link_incident_to_commit("INC-300", "sha-fix", "org/svc3", "fixed_by")
        trail = get_incident_audit_trail("INC-300")
        assert trail["incident_id"] == "INC-300"
        assert len(trail["caused_by_commits"]) == 1
        assert len(trail["fixed_by_commits"]) == 1
        assert trail["total_links"] == 2

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("INCIDENT_GIT_LINKER_ENABLED", "false")
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", False)

        result = mod.link_incident_to_commit("INC-999", "sha", "org/r", "caused_by")
        assert result is None

        commits = mod.get_commits_for_incident("INC-999")
        assert commits == []


# ---------------------------------------------------------------------------
# Coverage gap-fill tests
# ---------------------------------------------------------------------------

class TestGetIncidentsForCommitRelationshipFilter:
    """Cover line 146: get_incidents_for_commit with relationship filter."""

    def test_relationship_filter_in_reverse_lookup(self):
        from supervisor.incident_git_linker import _GitIncidentIndex, GitLink
        idx = _GitIncidentIndex()
        sha = "deadbeef12345678"
        idx.link(GitLink("INC-A", sha, "org/r", "caused_by"))
        idx.link(GitLink("INC-B", sha, "org/r", "fixed_by"))

        caused = idx.get_incidents_for_commit(sha, "caused_by")
        fixed  = idx.get_incidents_for_commit(sha, "fixed_by")

        assert all(lnk.relationship == "caused_by" for lnk in caused)
        assert all(lnk.relationship == "fixed_by"  for lnk in fixed)
        assert len(caused) == 1
        assert len(fixed)  == 1


class TestCorruptJsonStartsFresh:
    """Cover lines 227-228: corrupt JSON index file falls back gracefully."""

    def test_corrupt_json_starts_fresh(self, tmp_path, monkeypatch, caplog):
        import supervisor.incident_git_linker as mod
        index_path = str(tmp_path / "corrupt_idx.json")

        # Write garbage JSON
        with open(index_path, "w") as f:
            f.write("NOT VALID JSON {{{")

        monkeypatch.setattr(mod, "INDEX_PATH", index_path)
        monkeypatch.setattr(mod, "_index", None)  # reset singleton

        # _get_index() should log a warning and return a fresh empty index
        idx = mod._get_index()
        assert idx.stats()["total_links"] == 0


class TestLinkSaveFailure:
    """Cover lines 278-279: save() failure is logged, not re-raised."""

    def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", True)
        monkeypatch.setattr(mod, "_index", None)
        monkeypatch.setattr(mod, "INDEX_PATH", str(tmp_path / "idx.json"))

        def bad_save(self, path=None):
            raise OSError("disk full")

        with patch.object(mod._GitIncidentIndex, "save", bad_save):
            link = mod.link_incident_to_commit(
                "INC-SAVE-FAIL", "aabbccdd", "org/r", "caused_by", save=True
            )

        # Link was still recorded in memory even though save failed
        assert link is not None
        assert link.incident_id == "INC-SAVE-FAIL"


class TestDisabledLinkerPaths:
    """Cover lines 307, 315, 331-333: disabled linker returns empty/default values."""

    def test_disabled_get_incidents_for_commit(self, monkeypatch):
        """Cover line 307."""
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", False)
        result = mod.get_incidents_for_commit("any-sha")
        assert result == []

    def test_disabled_get_incident_audit_trail(self, monkeypatch):
        """Cover line 315."""
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", False)
        trail = mod.get_incident_audit_trail("INC-DISABLED")
        assert trail["incident_id"] == "INC-DISABLED"
        assert trail["enabled"] is False

    def test_disabled_index_stats(self, monkeypatch):
        """Cover line 332 (disabled path)."""
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", False)
        stats = mod.index_stats()
        assert stats == {}

    def test_enabled_index_stats(self, tmp_path, monkeypatch):
        """Cover line 333 (enabled path): module-level index_stats() call."""
        import supervisor.incident_git_linker as mod
        monkeypatch.setattr(mod, "LINKER_ENABLED", True)
        monkeypatch.setattr(mod, "_index", None)
        monkeypatch.setattr(mod, "INDEX_PATH", str(tmp_path / "stats_idx.json"))

        mod.link_incident_to_commit("INC-STATS", "sha-st", "org/r", "caused_by")
        stats = mod.index_stats()
        assert stats["total_links"] >= 1
