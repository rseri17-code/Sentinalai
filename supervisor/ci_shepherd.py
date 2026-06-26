"""CI Shepherd — monitors PR CI pipeline and auto-fixes failures.

After the DevLoopAgent creates a PR, CIShepherd:
  1. Polls GitHub CI status for the PR branch
  2. On failure: downloads error output, asks Claude to generate a fix
  3. Pushes the fix commit and waits for CI to re-run
  4. Repeats up to MAX_CI_FIX_ATTEMPTS
  5. If CI passes → signals success
  6. If still failing after all attempts → signals NEEDS_HUMAN

CIShepherd also acts as a standalone component: external systems can call
watch() directly for any PR, not just ones created by DevLoopAgent.

Architecture:
  - Synchronous (runs in thread pool from async caller)
  - Polls every CI_POLL_INTERVAL_SEC seconds
  - Max runtime: CI_POLL_INTERVAL_SEC × CI_MAX_POLLS

Configuration:
  CI_POLL_INTERVAL_SEC   — seconds between CI status polls (default: 30)
  CI_MAX_POLLS           — max polls before giving up (default: 40 = 20 min)
  CI_MAX_FIX_ATTEMPTS    — max auto-fix iterations (default: 3)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from sentinel_core.models.dev_task import DevTask, DevTaskStatus, CIRun
from sentinel_core.models.events import AGUIEvent, EventType

logger = logging.getLogger("sentinalai.ci_shepherd")

CI_POLL_INTERVAL_SEC = int(os.environ.get("CI_POLL_INTERVAL_SEC", "30"))
CI_MAX_POLLS         = int(os.environ.get("CI_MAX_POLLS", "40"))
CI_MAX_FIX_ATTEMPTS  = int(os.environ.get("CI_MAX_FIX_ATTEMPTS", "3"))


class CIShepherd:
    """Monitors PR CI and auto-fixes failures."""

    def watch(self, task: DevTask) -> dict[str, Any]:
        """Watch CI for a PR until it passes or we give up.

        Returns {"success": bool, "reason": str, "ci_runs": list}
        """
        if not task.pr_number:
            logger.info("CIShepherd: no PR number for task %s, skipping", task.task_id)
            return {"success": True, "reason": "no_pr"}

        logger.info(
            "CIShepherd watching PR #%s for task %s (max_polls=%d interval=%ds)",
            task.pr_number, task.task_id, CI_MAX_POLLS, CI_POLL_INTERVAL_SEC,
        )
        self._emit(task, EventType.DEV_CI_STARTED, {
            "pr_number": task.pr_number,
            "pr_url": task.pr_url,
            "max_polls": CI_MAX_POLLS,
        })

        for poll in range(1, CI_MAX_POLLS + 1):
            time.sleep(CI_POLL_INTERVAL_SEC)

            ci_status = self._get_ci_status(task)
            run = CIRun(
                run_id=ci_status.get("run_id", ""),
                status=ci_status.get("status", "unknown"),
                conclusion=ci_status.get("conclusion", ""),
                failed_jobs=ci_status.get("failed_jobs", []),
                url=ci_status.get("url", ""),
                fix_attempt=task.ci_fix_attempts,
            )
            task.ci_runs.append(run)

            logger.info(
                "CIShepherd poll %d/%d: PR #%s status=%s conclusion=%s",
                poll, CI_MAX_POLLS, task.pr_number, run.status, run.conclusion,
            )

            if run.status in ("queued", "in_progress", ""):
                continue  # still running

            if run.conclusion == "success":
                self._emit(task, EventType.DEV_CI_PASSED, {
                    "pr_number": task.pr_number,
                    "polls": poll,
                    "fix_attempts": task.ci_fix_attempts,
                })
                logger.info("CIShepherd: CI passed for PR #%s", task.pr_number)
                return {"success": True, "reason": "ci_passed", "ci_runs": len(task.ci_runs)}

            if run.conclusion in ("failure", "timed_out", "cancelled"):
                self._emit(task, EventType.DEV_CI_FAILED, {
                    "pr_number": task.pr_number,
                    "failed_jobs": run.failed_jobs,
                    "fix_attempt": task.ci_fix_attempts,
                })

                if not task.can_retry_ci:
                    return {
                        "success": False,
                        "reason": f"CI failed after {task.ci_fix_attempts} fix attempts",
                        "ci_runs": len(task.ci_runs),
                    }

                # Auto-fix
                self._emit(task, EventType.DEV_CI_FIXING, {
                    "fix_attempt": task.ci_fix_attempts + 1,
                    "failed_jobs": run.failed_jobs,
                })
                fixed = self._auto_fix_ci(task, run)
                task.ci_fix_attempts += 1

                if not fixed:
                    return {
                        "success": False,
                        "reason": "CI auto-fix failed to produce a valid patch",
                        "ci_runs": len(task.ci_runs),
                    }
                # CI will re-trigger; continue polling

        return {
            "success": False,
            "reason": f"CI did not complete after {CI_MAX_POLLS} polls ({CI_MAX_POLLS * CI_POLL_INTERVAL_SEC}s)",
            "ci_runs": len(task.ci_runs),
        }

    # ------------------------------------------------------------------
    # CI status polling
    # ------------------------------------------------------------------

    def _get_ci_status(self, task: DevTask) -> dict[str, Any]:
        """Fetch current CI status for the PR via GitHub MCP."""
        try:
            from workers.mcp_client import MCPClient
            client = MCPClient()
            result = client.call(
                "github.get_workflow_runs",
                {
                    "pr_number": task.pr_number,
                    "branch": task.pr_branch,
                    "limit": 1,
                },
            )
            runs = result.get("workflow_runs", result.get("runs", []))
            if runs:
                latest = runs[0]
                return {
                    "run_id": str(latest.get("id", "")),
                    "status": latest.get("status", ""),
                    "conclusion": latest.get("conclusion", "") or "",
                    "failed_jobs": self._get_failed_jobs(latest),
                    "url": latest.get("html_url", ""),
                }
            return {"status": "queued", "conclusion": ""}
        except Exception as exc:
            logger.debug("CI status poll failed: %s", exc)
            return {"status": "queued", "conclusion": ""}

    def _get_failed_jobs(self, run: dict) -> list[str]:
        """Extract failed job names from a workflow run."""
        jobs = run.get("jobs", [])
        return [j.get("name", "") for j in jobs if j.get("conclusion") == "failure"]

    # ------------------------------------------------------------------
    # Auto-fix
    # ------------------------------------------------------------------

    def _auto_fix_ci(self, task: DevTask, run: CIRun) -> bool:
        """Ask Claude to fix the CI failure and push a fixup commit."""
        error_output = self._fetch_ci_logs(task, run)
        if not error_output:
            return False

        fix_code = self._generate_ci_fix(task, run, error_output)
        if not fix_code or "[LLM unavailable" in fix_code:
            return False

        return self._apply_and_push_fix(task, fix_code, run)

    def _fetch_ci_logs(self, task: DevTask, run: CIRun) -> str:
        """Fetch CI failure logs from GitHub."""
        try:
            from workers.mcp_client import MCPClient
            client = MCPClient()
            result = client.call(
                "github.get_workflow_runs",
                {
                    "run_id": run.run_id,
                    "include_logs": True,
                    "pr_number": task.pr_number,
                },
            )
            logs = result.get("logs", result.get("log_output", ""))
            return str(logs)[-4000:] if logs else ""
        except Exception as exc:
            logger.debug("CI log fetch failed: %s", exc)
            return "\n".join(run.failed_jobs)

    def _generate_ci_fix(self, task: DevTask, run: CIRun, error_output: str) -> str:
        """Ask Claude to generate a fix for the CI failure."""
        prompt = f"""You are fixing a CI failure in a pull request.

PR: {task.pr_url}
Task: {task.title}
Failed jobs: {', '.join(run.failed_jobs) or 'unknown'}

CI error output:
{error_output[:3000]}

Files changed in this PR:
{chr(10).join(task.affected_files[:10])}

Generate ONLY the corrected file contents that fix the CI failure.
Use the format: ```path/to/file.py\\n<corrected content>\\n```
Do not explain. Just write the corrected code."""

        try:
            import anthropic
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=os.environ.get("DEV_LOOP_MODEL", "claude-sonnet-4-6"),
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text if response.content else ""
        except Exception as exc:
            logger.warning("CI fix LLM call failed: %s", exc)
            return f"[LLM unavailable: {exc}]"

    def _apply_and_push_fix(self, task: DevTask, fix_code: str, run: CIRun) -> bool:
        """Apply code changes from CI fix and push a fixup commit."""
        import re
        import subprocess

        repo_root = os.environ.get("REPO_ROOT", os.path.join(os.path.dirname(__file__), ".."))
        pattern = r"```(?:[\w.]+\n)?([\w./\-]+\.[a-zA-Z]+)\n(.*?)```"
        matches = re.findall(pattern, fix_code, re.DOTALL)

        if not matches:
            return False

        for file_path, content in matches:
            abs_path = os.path.join(repo_root, file_path.strip())
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w") as f:
                    f.write(content.strip() + "\n")
            except Exception as exc:
                logger.warning("CI fix write failed for %s: %s", file_path, exc)
                return False

        try:
            subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"fix: auto-fix CI failure (attempt {task.ci_fix_attempts + 1}) [sentinal-auto]"],
                cwd=repo_root, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "origin", task.pr_branch],
                cwd=repo_root, check=True, capture_output=True,
            )
            logger.info("CIShepherd: pushed CI fix for PR #%s", task.pr_number)
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning("CIShepherd: git push failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit(self, task: DevTask, event_type: EventType, payload: dict) -> None:
        try:
            import asyncio
            from agui.event_bus import get_bus
            event = AGUIEvent(
                event_type=event_type,
                investigation_id=task.task_id,
                incident_id=task.source_id or task.task_id,
                payload=payload,
            )
            loop = asyncio.new_event_loop()
            loop.run_until_complete(get_bus().publish(event))
            loop.close()
        except Exception:
            pass
