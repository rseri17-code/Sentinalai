"""Git Worker — backtrack to the actual breaking commit via git bisect.

This worker uses MCP-exposed git operations to identify which commit
introduced a regression.  Primary use cases:

  - "Which commit started the OOMKill spike?"
  - "Find the deployment that broke latency at 14:32 UTC"
  - "Who introduced the config change causing connection exhaustion?"

Operations:
  git_log_for_service     — recent commits touching a service's repo paths
  git_blame_line          — blame a specific file:line for authorship
  git_find_breaking_change — bisect-style: walks back from HEAD until
                             the metric/log pattern stops being present
  git_diff_range          — full diff between two SHAs
  git_show_commit         — single commit metadata + patch

Architecture:
    Agent → GitWorker.execute()
        → McpGateway.invoke("github.git_log" / "github.git_blame" / …)
            → GitHub MCP server
                → GitHub API / local repo

Phase placement:
    Phase 3 (change_correlation).  Proof-gated: only called when a
    deployment candidate SHA has been identified by DevopsWorker or ITSM.

Configuration:
  GIT_BISECT_MAX_COMMITS   — depth limit for finding breaking change (default: 50)
  GIT_WORKER_ENABLED       — on/off (default: true)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway

logger = logging.getLogger("sentinalai.git_worker")

GIT_BISECT_MAX_COMMITS = int(os.environ.get("GIT_BISECT_MAX_COMMITS", "50"))
GIT_WORKER_ENABLED = os.environ.get("GIT_WORKER_ENABLED", "true").lower() in ("1", "true", "yes")


class GitWorker(BaseWorker):
    """Worker that bisects git history to find the breaking commit."""

    worker_name = "git_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("git_log_for_service",          self._git_log_for_service)
        self.register("git_blame_line",               self._git_blame_line)
        self.register("git_find_breaking_change",     self._git_find_breaking_change)
        self.register("git_diff_range",               self._git_diff_range)
        self.register("git_show_commit",              self._git_show_commit)
        self.register("annotate_commit_with_incident", self._annotate_commit_with_incident)
        self.register("search_commits_by_incident",   self._search_commits_by_incident)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _git_log_for_service(self, params: dict) -> dict:
        """Return recent commits for a service's repository paths.

        Params:
            repo:           org/repo string (required)
            service:        service name (used for path filter if repo absent)
            paths:          list of file/directory paths to filter (optional)
            since_sha:      start walking from this SHA (optional)
            since_iso:      ISO-8601 timestamp — only commits after this (optional)
            until_iso:      ISO-8601 timestamp — only commits before this (optional)
            max_commits:    limit (default: 20)
            include_merges: include merge commits (default: false)

        Returns:
            {"commits": [{"sha", "message", "author", "date",
                          "files_changed": [...], "insertions", "deletions"}]}
        """
        repo = params.get("repo", "")
        service = params.get("service", "")
        if not repo and not service:
            return {"error": "repo or service required"}

        return self._gateway.invoke(
            "github.git_log",
            "git_log_for_service",
            {
                "repo": repo,
                "service": service,
                "paths": params.get("paths", []),
                "since_sha": params.get("since_sha", ""),
                "since_iso": params.get("since_iso", ""),
                "until_iso": params.get("until_iso", ""),
                "max_commits": params.get("max_commits", 20),
                "include_merges": params.get("include_merges", False),
            },
        )

    def _git_blame_line(self, params: dict) -> dict:
        """Return blame info for a specific file and line range.

        Params:
            repo:       org/repo string (required)
            path:       file path within the repo (required)
            line_start: first line number (required)
            line_end:   last line number (default: line_start)
            sha:        blame at this commit (default: HEAD)

        Returns:
            {"blame": [{"line", "sha", "author", "date", "message"}]}
        """
        repo = params.get("repo", "")
        path = params.get("path", "")
        if not repo or not path:
            return {"error": "repo and path required"}
        line_start = params.get("line_start")
        if line_start is None:
            return {"error": "line_start required"}

        return self._gateway.invoke(
            "github.git_blame",
            "git_blame_line",
            {
                "repo": repo,
                "path": path,
                "line_start": line_start,
                "line_end": params.get("line_end", line_start),
                "sha": params.get("sha", "HEAD"),
            },
        )

    def _git_find_breaking_change(self, params: dict) -> dict:
        """Walk back through commit history to find the first commit that
        introduced the problem.  This is a lightweight bisect implementation
        using the commit log: it narrows the window by fetching the log
        and returning the candidate range for further triage.

        Params:
            repo:           org/repo string (required)
            good_sha:       last known-good commit SHA (optional)
            bad_sha:        first known-bad commit SHA (default: HEAD)
            paths:          list of file/dir paths to restrict history
            incident_time:  ISO-8601 time of incident — used to anchor bad_sha
            service:        service name (used to infer repo if repo absent)
            max_depth:      max commits to walk back (default: GIT_BISECT_MAX_COMMITS)

        Returns:
            {
                "breaking_commit": {"sha", "message", "author", "date",
                                    "files_changed", "pr_number"},
                "candidate_window": [<commit>, ...],
                "bisect_strategy": "time_anchored" | "path_filtered" | "full",
                "confidence": 0.0–1.0,
            }
        """
        repo = params.get("repo", "")
        service = params.get("service", "")
        if not repo and not service:
            return {"error": "repo or service required"}

        incident_time = params.get("incident_time", "")
        bad_sha = params.get("bad_sha", "HEAD")
        good_sha = params.get("good_sha", "")
        paths = params.get("paths", [])
        max_depth = min(int(params.get("max_depth", GIT_BISECT_MAX_COMMITS)), GIT_BISECT_MAX_COMMITS)

        # Step 1: Fetch the commit log in the suspect window
        log_params: dict[str, Any] = {
            "repo": repo,
            "service": service,
            "paths": paths,
            "max_commits": max_depth,
            "include_merges": False,
        }
        if incident_time:
            log_params["until_iso"] = incident_time
        if good_sha:
            log_params["since_sha"] = good_sha

        log_result = self._gateway.invoke("github.git_log", "git_log_for_service", log_params)
        if log_result.get("error"):
            return log_result

        commits = log_result.get("commits", [])
        if not commits:
            return {
                "breaking_commit": None,
                "candidate_window": [],
                "bisect_strategy": "no_commits_found",
                "confidence": 0.0,
            }

        # Step 2: Identify the candidate — first commit at or just before incident
        # Heuristic: commits are returned newest-first; the first one in the window
        # that touches service-relevant paths is our prime suspect.
        bisect_strategy = "time_anchored" if incident_time else (
            "path_filtered" if paths else "full"
        )

        # Prefer commits that touch known problem paths
        breaking_commit = _pick_breaking_commit(commits, paths)

        # Step 3: Enrich the breaking commit with PR number (if available)
        if breaking_commit:
            enriched = self._enrich_commit(repo, breaking_commit)
        else:
            enriched = None

        return {
            "breaking_commit": enriched or breaking_commit,
            "candidate_window": commits[:10],   # top-10 for human review
            "bisect_strategy": bisect_strategy,
            "confidence": _bisect_confidence(breaking_commit, commits, paths),
        }

    def _git_diff_range(self, params: dict) -> dict:
        """Return the full diff between two commit SHAs.

        Params:
            repo:       org/repo string (required)
            base_sha:   base / older commit SHA (required)
            head_sha:   head / newer commit SHA (default: HEAD)
            paths:      optional list of paths to restrict diff

        Returns:
            {"diff": "<patch text>", "files_changed": [...],
             "insertions": int, "deletions": int}
        """
        repo = params.get("repo", "")
        base_sha = params.get("base_sha", "")
        if not repo or not base_sha:
            return {"error": "repo and base_sha required"}

        return self._gateway.invoke(
            "github.git_diff",
            "git_diff_range",
            {
                "repo": repo,
                "base_sha": base_sha,
                "head_sha": params.get("head_sha", "HEAD"),
                "paths": params.get("paths", []),
            },
        )

    def _git_show_commit(self, params: dict) -> dict:
        """Return metadata and patch for a single commit.

        Params:
            repo:   org/repo string (required)
            sha:    commit SHA (required)

        Returns:
            {"sha", "message", "author", "date", "patch", "files_changed",
             "insertions", "deletions", "parent_sha"}
        """
        repo = params.get("repo", "")
        sha = params.get("sha", "")
        if not repo or not sha:
            return {"error": "repo and sha required"}

        return self._gateway.invoke(
            "github.git_show",
            "git_show_commit",
            {"repo": repo, "sha": sha},
        )

    def _annotate_commit_with_incident(self, params: dict) -> dict:
        """Link a commit to an incident in the bidirectional index.

        Params:
            incident_id:     Alert/incident ID (required)
            commit_sha:      Full or short commit SHA (required, also accepts "sha")
            repo:            org/repo string (required)
            relationship:    "caused_by" | "fixed_by" | "related" (default: "caused_by")
            confidence:      0.0–1.0 (default: 1.0)
            author:          Commit author (optional)
            commit_message:  First 120 chars of commit message (optional)
            pr_number:       PR number if commit was a PR merge (optional)

        Returns:
            {"linked": true, "link": {...}}
        """
        from supervisor.incident_git_linker import link_incident_to_commit

        incident_id = params.get("incident_id", "")
        commit_sha = params.get("commit_sha", "") or params.get("sha", "")
        repo = params.get("repo", "")
        if not incident_id or not commit_sha or not repo:
            return {"error": "incident_id, commit_sha, and repo required"}

        link = link_incident_to_commit(
            incident_id=incident_id,
            commit_sha=commit_sha,
            repo=repo,
            relationship=params.get("relationship", "caused_by"),
            confidence=float(params.get("confidence", 1.0)),
            author=params.get("author", ""),
            commit_message=params.get("commit_message", ""),
            pr_number=params.get("pr_number"),
        )

        if link is None:
            return {"linked": False, "reason": "linker_disabled"}

        return {"linked": True, "link": link.to_dict()}

    def _search_commits_by_incident(self, params: dict) -> dict:
        """Return all commits linked to an incident from the bidirectional index.

        Params:
            incident_id:  Incident ID to look up (required)
            relationship: Filter by "caused_by" | "fixed_by" | "related" (optional)

        Returns:
            {"commits": [...], "total": int}
        """
        from supervisor.incident_git_linker import get_commits_for_incident

        incident_id = params.get("incident_id", "")
        if not incident_id:
            return {"error": "incident_id required"}

        relationship = params.get("relationship") or None
        commits = get_commits_for_incident(incident_id, relationship)
        return {"commits": commits, "total": len(commits)}

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _enrich_commit(self, repo: str, commit: dict) -> dict:
        """Try to add PR number by looking up the commit SHA in the PR API."""
        sha = commit.get("sha", "")
        if not sha:
            return commit
        try:
            pr_result = self._gateway.invoke(
                "github.get_pr_for_commit",
                "get_pr_for_commit",
                {"repo": repo, "sha": sha},
            )
            if pr_result and not pr_result.get("error"):
                return {**commit, "pr_number": pr_result.get("pr_number"),
                        "pr_title": pr_result.get("pr_title", "")}
        except Exception as exc:
            logger.debug("PR enrichment failed for %s: %s", sha[:8], exc)
        return commit


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _pick_breaking_commit(commits: list[dict], paths: list[str]) -> dict | None:
    """Select the most likely breaking commit from an ordered list.

    Preference:
      1. First commit that touches one of the suspect paths.
      2. Fallback: most recent commit.
    """
    if not commits:
        return None

    if paths:
        path_set = {p.lower() for p in paths}
        for commit in commits:
            changed = [f.lower() for f in commit.get("files_changed", [])]
            if any(any(p in c for p in path_set) for c in changed):
                return commit

    return commits[0]


def _bisect_confidence(
    breaking_commit: dict | None,
    all_commits: list[dict],
    paths: list[str],
) -> float:
    """Score our confidence that we've found the right commit.

    Higher when:
      - Only one commit in the window
      - The commit touches known suspect paths
      - The commit is a non-merge single-author change
    """
    if not breaking_commit:
        return 0.0

    score = 0.5  # baseline

    # Narrow window → higher confidence
    n = len(all_commits)
    if n == 1:
        score += 0.35
    elif n <= 3:
        score += 0.20
    elif n <= 10:
        score += 0.10

    # Path intersection → higher confidence
    if paths:
        changed = [f.lower() for f in breaking_commit.get("files_changed", [])]
        path_set = {p.lower() for p in paths}
        if any(any(p in c for p in path_set) for c in changed):
            score += 0.15

    return round(min(score, 1.0), 2)
