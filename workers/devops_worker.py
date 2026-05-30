"""DevOps Worker - handles GitHub CI/CD and code change operations.

Calls MCP server via AgentCore gateway when configured.
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

from __future__ import annotations

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway


class DevopsWorker(BaseWorker):
    """Worker that interfaces with GitHub for CI/CD and code change data."""

    worker_name = "devops_worker"

    def __init__(self, gateway: McpGateway | None = None):
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("get_recent_deployments", self._get_recent_deployments)
        self.register("get_pr_details", self._get_pr_details)
        self.register("get_commit_diff", self._get_commit_diff)
        self.register("get_workflow_runs", self._get_workflow_runs)
        self.register("create_fix_pr", self._create_fix_pr)
        self.register("rollback_deployment", self._rollback_deployment)
        self.register("scale_service", self._scale_service)
        # Registered as backing implementation for change_worker alias
        self.register("get_config_changes", self._get_config_changes)

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
        return self._gateway.invoke(
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
        return self._gateway.invoke(
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
        return self._gateway.invoke(
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
        return self._gateway.invoke(
            "github.get_workflow_runs",
            "get_workflow_runs",
            {
                "service": service,
                "repo": repo,
                "sha": params.get("sha", ""),
                "time_window_hours": params.get("time_window_hours", 24),
            },
        )

    def _create_fix_pr(self, params: dict) -> dict:
        """Create a GitHub pull request with a generated code fix.

        Params:
            repo:      org/repo string
            title:     PR title (e.g. "fix: remove null check causing NPE")
            body:      PR description (markdown, includes RCA context)
            patch:     Unified diff of the fix
            base_sha:  The commit SHA the fix is based on
            actor_id:  Who triggered the fix (for audit)
            branch:    Optional branch name (auto-generated if omitted)

        Returns:
            {"pr": {"number", "html_url", "branch", "sha"}}
        """
        repo = params.get("repo", "")
        title = params.get("title", "")
        if not repo or not title:
            return {"error": "repo and title required"}
        return self._gateway.invoke(
            "github.create_fix_pr",
            "create_fix_pr",
            {
                "repo": repo,
                "title": title,
                "body": params.get("body", ""),
                "patch": params.get("patch", ""),
                "base_sha": params.get("base_sha", ""),
                "actor_id": params.get("actor_id", "sentinalai"),
                "branch": params.get("branch", ""),
            },
        )

    def _rollback_deployment(self, params: dict) -> dict:
        """Rollback a Kubernetes deployment to the previous revision.

        Params:
            service:   Service name (maps to k8s deployment name)
            repo:      Optional org/repo (for GitHub Actions rollback)
            sha:       The SHA being rolled back FROM (for audit trail)
            command:   Optional explicit kubectl command (overrides auto-generate)
            actor_id:  Who triggered the rollback (for audit)
            namespace: Kubernetes namespace (default: production)

        Returns:
            {"rollback": {"status", "previous_revision", "deployment", "message"}}
        """
        service = params.get("service", "")
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "kubernetes.rollback_deployment",
            "rollback_deployment",
            {
                "service": service,
                "repo": params.get("repo", ""),
                "sha": params.get("sha", ""),
                "command": params.get("command", f"kubectl rollout undo deployment/{service}"),
                "actor_id": params.get("actor_id", "sentinalai"),
                "namespace": params.get("namespace", "production"),
            },
        )

    def _get_config_changes(self, params: dict) -> dict:
        """Retrieve recent config file changes from GitHub for a service repo.

        Params:
            service: Service name
            repo: Optional org/repo override
            time_window_hours: Lookback window (default 24)

        Returns:
            {"config_changes": [{"filename", "patch", "sha", "author", "merged_at"}]}
        """
        service = params.get("service", "")
        repo = params.get("repo", "")
        if not service and not repo:
            return {"error": "service or repo required"}
        return self._gateway.invoke(
            "github.get_config_changes",
            "get_config_changes",
            {
                "service": service,
                "repo": repo,
                "time_window_hours": params.get("time_window_hours", 24),
            },
        )

    def _scale_service(self, params: dict) -> dict:
        """Scale a Kubernetes deployment to a target replica count.

        Params:
            service:   Service name (k8s deployment name)
            replicas:  Target replica count
            actor_id:  Who triggered the scale action
            namespace: Kubernetes namespace (default: production)

        Returns:
            {"scale": {"status", "replicas", "deployment"}}
        """
        service = params.get("service", "")
        replicas = params.get("replicas", 2)
        if not service:
            return {"error": "service required"}
        return self._gateway.invoke(
            "kubernetes.scale_service",
            "scale_service",
            {
                "service": service,
                "replicas": replicas,
                "actor_id": params.get("actor_id", "sentinalai"),
                "namespace": params.get("namespace", "production"),
            },
        )
