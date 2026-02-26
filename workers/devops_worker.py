"""DevOps Worker - handles GitHub CI/CD and code change operations.

Calls MCP server via AgentCore tool ARN when MCP_GITHUB_TOOL_ARN is set.
Falls back to stub response for local dev / testing.

Provides:
- Recent deployments: merged PRs, releases, and tags in a time window
- PR details: files changed, CI status, review approvals
- Commit diff: targeted diff for a specific commit SHA
- Workflow runs: GitHub Actions CI/CD pipeline status

Architecture:
    Agent -> AgentCore Gateway -> GitHub MCP Runtime Engine -> GitHub API

Phase placement:
    - All actions are Phase 3 (change_correlation), proof-gated.
    - Only called when _find_deployment() already identified a deployment
      in ITSM/Splunk change data. Never used for speculative code trawls.
"""

from workers.base_worker import BaseWorker
from workers.mcp_client import invoke_mcp_tool


class DevopsWorker(BaseWorker):
    """Worker that interfaces with GitHub for CI/CD and code change data."""

    worker_name = "devops_worker"

    def __init__(self):
        super().__init__()
        self.register("get_recent_deployments", self._get_recent_deployments)
        self.register("get_pr_details", self._get_pr_details)
        self.register("get_commit_diff", self._get_commit_diff)
        self.register("get_workflow_runs", self._get_workflow_runs)

    def _get_recent_deployments(self, params: dict) -> dict:
        """List recent deployments (merged PRs, releases) for a service repo.

        Params:
            service: Service name (maps to repo via CMDB convention)
            repo: Optional explicit org/repo (overrides service mapping)
            time_window_hours: Lookback window (default 24)

        Returns:
            {"deployments": [{"sha", "ref", "description", "author",
                              "merged_at", "pr_number", "release_tag",
                              "environment"}]}
        """
        service = params.get("service", "")
        repo = params.get("repo", "")
        if not service and not repo:
            return {"error": "service or repo required"}
        return invoke_mcp_tool(
            "github.get_recent_deployments",
            "get_recent_deployments",
            {
                "service": service,
                "repo": repo,
                "time_window_hours": params.get("time_window_hours", 24),
            },
        )

    def _get_pr_details(self, params: dict) -> dict:
        """Retrieve pull request details including files changed and CI status.

        Params:
            repo: org/repo string
            pr_number: Pull request number

        Returns:
            {"pr": {"number", "title", "author", "merged_at",
                    "files_changed": [{"filename", "additions", "deletions"}],
                    "ci_status", "review_state", "labels"}}
        """
        repo = params.get("repo", "")
        pr_number = params.get("pr_number")
        if not repo or not pr_number:
            return {"error": "repo and pr_number required"}
        return invoke_mcp_tool(
            "github.get_pr_details",
            "get_pr_details",
            {"repo": repo, "pr_number": pr_number},
        )

    def _get_commit_diff(self, params: dict) -> dict:
        """Retrieve the diff for a specific commit SHA.

        Params:
            repo: org/repo string
            sha: Commit SHA

        Returns:
            {"commit": {"sha", "message", "author", "date",
                        "files": [{"filename", "patch", "additions",
                                   "deletions"}],
                        "stats": {"total", "additions", "deletions"}}}
        """
        repo = params.get("repo", "")
        sha = params.get("sha", "")
        if not repo or not sha:
            return {"error": "repo and sha required"}
        return invoke_mcp_tool(
            "github.get_commit_diff",
            "get_commit_diff",
            {"repo": repo, "sha": sha},
        )

    def _get_workflow_runs(self, params: dict) -> dict:
        """Retrieve recent GitHub Actions workflow runs for a service.

        Params:
            service: Service name (maps to repo via CMDB convention)
            repo: Optional explicit org/repo
            sha: Optional commit SHA to filter by
            time_window_hours: Lookback window (default 24)

        Returns:
            {"workflow_runs": [{"id", "name", "status", "conclusion",
                                "head_sha", "created_at", "updated_at",
                                "html_url"}]}
        """
        service = params.get("service", "")
        repo = params.get("repo", "")
        if not service and not repo:
            return {"error": "service or repo required"}
        return invoke_mcp_tool(
            "github.get_workflow_runs",
            "get_workflow_runs",
            {
                "service": service,
                "repo": repo,
                "sha": params.get("sha", ""),
                "time_window_hours": params.get("time_window_hours", 24),
            },
        )
