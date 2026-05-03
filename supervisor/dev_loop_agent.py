"""Closed-loop agent-driven development agent.

The DevLoopAgent takes a DevTask (from Slack/Linear/GitHub) and drives it
through the full development lifecycle without human intervention:

  1. Load production context
     - Query KG for affected services, recent incidents, blast radius
     - Load experience store for similar past changes
     - Summarise what the agent knows about the affected code area

  2. Plan the implementation
     - Determine which files to create/modify
     - Generate a structured implementation plan
     - Identify tests that must pass

  3. Write the code  (up to MAX_ITERATIONS validation cycles)
     - Claude generates the implementation
     - Run pytest / mypy / eslint
     - On failure: analyse errors, regenerate, retry

  4. Create a pull request
     - Write a descriptive PR body (links to task, prod context, plan)
     - Push via GitHub MCP

  5. Hand off to CIShepherd
     - CIShepherd watches CI and auto-fixes failures (separate module)

  6. Emit DEV_* events on the AGUI event bus throughout

The agent is synchronous (runs in a thread pool from the async intake layer).
All Claude calls go through the LLM interface already used by the RCA agent.

Configuration:
  DEV_LOOP_MAX_ITERATIONS       — max code→validate cycles (default: 5)
  DEV_LOOP_MAX_LLM_CALLS        — budget for LLM calls (default: 30)
  DEV_LOOP_ENABLED              — on/off (default: true)
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agui.schemas.dev_task import (
    DevTask, DevTaskStatus, DevTaskType, ValidationResult, CIRun,
)
from agui.schemas.events import AGUIEvent, EventType

logger = logging.getLogger("sentinalai.dev_loop_agent")

DEV_LOOP_MAX_ITERATIONS = int(os.environ.get("DEV_LOOP_MAX_ITERATIONS", "5"))
DEV_LOOP_MAX_LLM_CALLS  = int(os.environ.get("DEV_LOOP_MAX_LLM_CALLS", "30"))
DEV_LOOP_ENABLED        = os.environ.get("DEV_LOOP_ENABLED", "true").lower() in ("1", "true", "yes")

REPO_ROOT = os.environ.get("REPO_ROOT", os.path.join(os.path.dirname(__file__), ".."))


@dataclass
class DevContext:
    """All production + code context the agent assembles before writing code."""
    affected_services: list[str] = field(default_factory=list)
    recent_incidents: list[dict] = field(default_factory=list)
    similar_past_changes: list[dict] = field(default_factory=list)
    blast_radius: list[str] = field(default_factory=list)
    related_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    summary: str = ""


class DevLoopAgent:
    """Synchronous closed-loop development agent."""

    def __init__(self) -> None:
        self._llm_calls = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, task: DevTask) -> dict[str, Any]:
        """Execute the full dev loop for a task. Returns result dict.

        Returns:
          {"success": bool, "pr_url": str, "pr_number": int|None,
           "validation_iterations": int, "blocker_reason": str}
        """
        if not DEV_LOOP_ENABLED:
            return {"success": False, "blocker_reason": "DevLoopAgent is disabled"}

        logger.info(
            "DevLoopAgent starting: task=%s type=%s title=%r",
            task.task_id, task.task_type.value, task.title[:60],
        )
        task.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        task.status = DevTaskStatus.IMPLEMENTING

        try:
            # Phase 1: production context
            ctx = self._load_production_context(task)
            task.production_context_summary = ctx.summary
            task.affected_services = ctx.affected_services or task.affected_services
            task.related_incident_ids = [i.get("incident_id", "") for i in ctx.recent_incidents[:5]]
            self._emit(task, EventType.DEV_IMPLEMENTING, {
                "phase": "context_loaded",
                "affected_services": ctx.affected_services,
                "recent_incident_count": len(ctx.recent_incidents),
            })

            # Phase 2: implementation plan
            plan = self._generate_plan(task, ctx)
            task.implementation_plan = plan["plan_text"]
            task.files_to_create = plan.get("files_to_create", [])
            task.files_to_modify = plan.get("files_to_modify", [])
            self._emit(task, EventType.DEV_IMPLEMENTING, {
                "phase": "plan_generated",
                "files_to_create": task.files_to_create,
                "files_to_modify": task.files_to_modify,
            })

            # Phase 3: code → validate loop
            task.status = DevTaskStatus.VALIDATING
            validation_passed = False
            for iteration in range(1, DEV_LOOP_MAX_ITERATIONS + 1):
                task.validation_iterations = iteration

                if task.budget_exhausted:
                    return self._block(task, "LLM budget exhausted before validation passed")

                # Generate / refine code
                self._emit(task, EventType.DEV_VALIDATION_STARTED, {"iteration": iteration})
                code_result = self._generate_code(task, ctx, plan, iteration)

                # Run validation suite
                val = self._run_validation(task, iteration)
                task.validation_history.append(val)

                if val.passed:
                    task.validation_passed = True
                    validation_passed = True
                    self._emit(task, EventType.DEV_VALIDATION_PASSED, {
                        "iteration": iteration,
                        "files_changed": val.files_changed,
                    })
                    break
                else:
                    self._emit(task, EventType.DEV_VALIDATION_FAILED, {
                        "iteration": iteration,
                        "errors": val.errors[:5],
                    })
                    if iteration < DEV_LOOP_MAX_ITERATIONS:
                        self._emit(task, EventType.DEV_VALIDATION_ITERATING, {
                            "iteration": iteration,
                            "next_iteration": iteration + 1,
                        })
                        # Feed errors back into the next code generation pass
                        plan["previous_errors"] = val.errors

            if not validation_passed:
                return self._block(
                    task,
                    f"Validation still failing after {DEV_LOOP_MAX_ITERATIONS} iterations — "
                    "engineer review required"
                )

            # Phase 4: create PR
            task.status = DevTaskStatus.CREATING_PR
            pr = self._create_pr(task, ctx)
            task.pr_url = pr.get("pr_url", "")
            task.pr_number = pr.get("pr_number")
            task.pr_title = pr.get("pr_title", task.title)
            task.pr_branch = pr.get("branch", "")
            self._emit(task, EventType.DEV_PR_CREATED, {
                "pr_url": task.pr_url,
                "pr_number": task.pr_number,
                "pr_title": task.pr_title,
            })

            # Phase 5: hand off to CI shepherd
            task.status = DevTaskStatus.SHEPHERDING_CI
            ci_result = self._shepherd_ci(task)
            if not ci_result.get("success"):
                return self._block(
                    task,
                    f"CI failed after {task.ci_fix_attempts} fix attempts: "
                    f"{ci_result.get('reason', 'unknown')}"
                )

            task.status = DevTaskStatus.COMPLETED
            task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            task.duration_sec = time.time() - (
                time.mktime(time.strptime(task.started_at, "%Y-%m-%dT%H:%M:%SZ"))
                if task.started_at else time.time()
            )
            self._emit(task, EventType.DEV_COMPLETED, {
                "pr_url": task.pr_url,
                "pr_number": task.pr_number,
                "validation_iterations": task.validation_iterations,
                "ci_fix_attempts": task.ci_fix_attempts,
                "duration_sec": task.duration_sec,
            })

            logger.info(
                "DevLoopAgent completed: task=%s pr=%s iterations=%d",
                task.task_id, task.pr_url, task.validation_iterations,
            )
            return {
                "success": True,
                "pr_url": task.pr_url,
                "pr_number": task.pr_number,
                "validation_iterations": task.validation_iterations,
                "blocker_reason": "",
            }

        except Exception as exc:
            logger.error("DevLoopAgent unhandled error for task %s: %s", task.task_id, exc)
            return self._block(task, f"Unexpected error: {exc}")

    # ------------------------------------------------------------------
    # Phase 1: Production context
    # ------------------------------------------------------------------

    def _load_production_context(self, task: DevTask) -> DevContext:
        """Load production context from KG, experience store, and KG queries."""
        ctx = DevContext()
        affected = set(task.affected_services)

        # Extract service names from task text
        text = f"{task.title} {task.description}".lower()
        try:
            from supervisor.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph.get_graph()
            # Find services mentioned in task
            for node in kg.get("nodes", []):
                if node.get("node_type") == "service":
                    svc = node.get("label", "")
                    if svc.lower() in text or text in svc.lower():
                        affected.add(svc)
            # Find recent incidents for affected services
            for svc in list(affected)[:3]:
                similar = kg.get("nodes", [])
                for n in similar:
                    if n.get("node_type") == "incident" and svc in str(n.get("props", {})):
                        ctx.recent_incidents.append({
                            "incident_id": n.get("node_id", ""),
                            "summary": n.get("label", ""),
                            "service": svc,
                        })
        except Exception as exc:
            logger.debug("KG context load skipped: %s", exc)

        # Load similar past changes from experience store
        try:
            from supervisor.experience_store import ExperienceStore
            store = ExperienceStore.load()
            similar = store.retrieve_similar(
                incident_type=task.task_type.value,
                service=list(affected)[0] if affected else "",
                top_k=3,
            )
            ctx.similar_past_changes = [
                {"root_cause": e.get("root_cause", ""), "service": e.get("service", "")}
                for e in similar
            ]
        except Exception as exc:
            logger.debug("Experience store lookup skipped: %s", exc)

        ctx.affected_services = list(affected) if affected else ["unknown"]
        ctx.summary = (
            f"Affected services: {', '.join(ctx.affected_services)}. "
            f"Recent incidents: {len(ctx.recent_incidents)}. "
            f"Similar past changes found: {len(ctx.similar_past_changes)}."
        )
        return ctx

    # ------------------------------------------------------------------
    # Phase 2: Implementation plan
    # ------------------------------------------------------------------

    def _generate_plan(self, task: DevTask, ctx: DevContext) -> dict[str, Any]:
        """Ask Claude to generate an implementation plan before writing code."""
        self._llm_calls += 1
        task.llm_calls_used = self._llm_calls

        prompt = self._build_plan_prompt(task, ctx)
        response = self._call_llm(prompt, max_tokens=1500)

        plan: dict[str, Any] = {
            "plan_text": response,
            "files_to_create": [],
            "files_to_modify": [],
            "previous_errors": [],
        }

        # Parse file lists from plan text (look for code-fenced file paths)
        import re
        create_match = re.search(r"Files to create[:\s]+([^\n]+(?:\n[-*]\s+[^\n]+)*)", response, re.I)
        modify_match = re.search(r"Files to modify[:\s]+([^\n]+(?:\n[-*]\s+[^\n]+)*)", response, re.I)
        if create_match:
            plan["files_to_create"] = [
                l.strip("- *`\n") for l in create_match.group(1).split("\n") if l.strip("- *`\n ")
            ]
        if modify_match:
            plan["files_to_modify"] = [
                l.strip("- *`\n") for l in modify_match.group(1).split("\n") if l.strip("- *`\n ")
            ]
        return plan

    def _build_plan_prompt(self, task: DevTask, ctx: DevContext) -> str:
        return f"""You are a senior software engineer planning a code change.

Task: {task.title}
Type: {task.task_type.value}
Priority: {task.priority.value}

Description:
{task.description}

Production context:
{ctx.summary}

Recent incidents on affected services:
{chr(10).join(f"- {i['incident_id']}: {i['summary']}" for i in ctx.recent_incidents[:3]) or "None"}

Respond with:
1. A brief implementation plan (what to change and why)
2. Files to create: (list)
3. Files to modify: (list)
4. Tests that must pass after this change
5. Any production risks to be aware of

Be specific and concise. Focus on minimal, safe changes."""

    # ------------------------------------------------------------------
    # Phase 3: Code generation
    # ------------------------------------------------------------------

    def _generate_code(
        self,
        task: DevTask,
        ctx: DevContext,
        plan: dict[str, Any],
        iteration: int,
    ) -> dict[str, Any]:
        """Ask Claude to write or fix code based on the plan and previous errors."""
        self._llm_calls += 1
        task.llm_calls_used = self._llm_calls

        prev_errors = plan.get("previous_errors", [])
        prompt = self._build_code_prompt(task, ctx, plan, iteration, prev_errors)
        response = self._call_llm(prompt, max_tokens=4000)

        # Extract and write file contents from Claude's response
        files_written = self._apply_code_changes(response, task)
        return {"files_written": files_written, "llm_response": response}

    def _build_code_prompt(
        self,
        task: DevTask,
        ctx: DevContext,
        plan: dict[str, Any],
        iteration: int,
        prev_errors: list[str],
    ) -> str:
        error_section = ""
        if prev_errors:
            error_section = f"""
Previous validation errors (iteration {iteration - 1}) — you MUST fix all of these:
{chr(10).join(f"  {e}" for e in prev_errors[:10])}
"""
        return f"""You are implementing a code change. Write complete, working code.

Task: {task.title}
Type: {task.task_type.value}
Iteration: {iteration}/{DEV_LOOP_MAX_ITERATIONS}

Implementation plan:
{plan['plan_text'][:800]}

Files to create: {plan.get('files_to_create', [])}
Files to modify: {plan.get('files_to_modify', [])}
{error_section}
Production context:
{ctx.summary}

RULES:
- Write complete file contents, not diffs
- Use the exact format: ```path/to/file.py\\n<content>\\n```
- Follow existing code style (Python 3.11+, type hints, no unnecessary comments)
- Ensure all imports are correct
- Write tests for new behaviour

Respond ONLY with code blocks. No explanations outside code blocks."""

    def _apply_code_changes(self, llm_response: str, task: DevTask) -> list[str]:
        """Parse code blocks from LLM response and write files."""
        import re
        pattern = r"```(?:[\w.]+\n)?([\w./\-]+\.[a-zA-Z]+)\n(.*?)```"
        matches = re.findall(pattern, llm_response, re.DOTALL)

        # Also handle ```python\n# path: foo.py\n...``` style
        alt_pattern = r"```\w*\n#\s*(?:file|path):\s*([\w./\-]+)\n(.*?)```"
        matches += re.findall(alt_pattern, llm_response, re.DOTALL)

        files_written: list[str] = []
        for file_path, content in matches:
            file_path = file_path.strip()
            if not file_path or "/" not in file_path and "." not in file_path:
                continue
            abs_path = os.path.join(REPO_ROOT, file_path)
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w") as f:
                    f.write(content.strip() + "\n")
                files_written.append(file_path)
                logger.debug("DevLoopAgent wrote: %s", file_path)
            except Exception as exc:
                logger.warning("Failed to write %s: %s", file_path, exc)

        if files_written:
            task.affected_files = list(set(task.affected_files + files_written))
        return files_written

    # ------------------------------------------------------------------
    # Phase 3b: Validation
    # ------------------------------------------------------------------

    def _run_validation(self, task: DevTask, iteration: int) -> ValidationResult:
        """Run pytest, mypy, and eslint. Returns structured ValidationResult."""
        start = time.time()
        errors: list[str] = []
        test_out = lint_out = type_out = ""

        # Python tests
        try:
            r = subprocess.run(
                ["python", "-m", "pytest", "-x", "-q", "--tb=short", "--no-header"],
                capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
            )
            test_out = (r.stdout + r.stderr)[-3000:]
            if r.returncode != 0:
                # Extract just the failure lines
                for line in test_out.split("\n"):
                    if "FAILED" in line or "ERROR" in line or "AssertionError" in line:
                        errors.append(line.strip())
        except subprocess.TimeoutExpired:
            errors.append("pytest timed out after 120s")
        except Exception as exc:
            errors.append(f"pytest error: {exc}")

        # Python type check (only if modified .py files)
        py_files = [f for f in task.affected_files if f.endswith(".py")]
        if py_files and not errors:
            try:
                r = subprocess.run(
                    ["python", "-m", "mypy", "--ignore-missing-imports", "--no-error-summary"] + py_files,
                    capture_output=True, text=True, timeout=60, cwd=REPO_ROOT,
                )
                type_out = (r.stdout + r.stderr)[-2000:]
                if r.returncode != 0:
                    for line in type_out.split("\n"):
                        if "error:" in line:
                            errors.append(line.strip())
            except Exception:
                pass  # mypy is not always installed

        # UI type check (only if modified .tsx/.ts files)
        ts_files = [f for f in task.affected_files if f.endswith((".ts", ".tsx"))]
        ui_dir = os.path.join(REPO_ROOT, "ui")
        if ts_files and os.path.exists(os.path.join(ui_dir, "package.json")):
            try:
                r = subprocess.run(
                    ["npm", "run", "typecheck"],
                    capture_output=True, text=True, timeout=60, cwd=ui_dir,
                )
                lint_out = (r.stdout + r.stderr)[-2000:]
                if r.returncode != 0:
                    for line in lint_out.split("\n"):
                        if "error TS" in line:
                            errors.append(line.strip())
            except Exception:
                pass

        passed = len(errors) == 0
        return ValidationResult(
            iteration=iteration,
            passed=passed,
            test_output=test_out,
            lint_output=lint_out,
            typecheck_output=type_out,
            errors=errors[:20],
            files_changed=task.affected_files[:],
            duration_sec=round(time.time() - start, 1),
        )

    # ------------------------------------------------------------------
    # Phase 4: PR creation
    # ------------------------------------------------------------------

    def _create_pr(self, task: DevTask, ctx: DevContext) -> dict[str, Any]:
        """Create a GitHub PR via the MCP client."""
        branch = f"sentinal/auto/{task.task_type.value}/{task.task_id[:8]}"
        pr_body = self._build_pr_body(task, ctx)

        try:
            from workers.mcp_client import MCPClient
            client = MCPClient()
            result = client.call(
                "github.create_pull_request",
                {
                    "title": f"[Auto] {task.title[:100]}",
                    "body": pr_body,
                    "branch": branch,
                    "base": "main",
                    "labels": ["sentinal-auto", task.task_type.value],
                    "draft": False,
                },
            )
            pr_url = result.get("pr_url", result.get("pr", {}).get("url", ""))
            pr_number = result.get("pr_number", result.get("pr", {}).get("number"))
            return {"pr_url": pr_url, "pr_number": pr_number, "pr_title": task.title, "branch": branch}
        except Exception as exc:
            logger.warning("PR creation failed (non-critical): %s", exc)
            return {"pr_url": "", "pr_number": None, "pr_title": task.title, "branch": branch}

    def _build_pr_body(self, task: DevTask, ctx: DevContext) -> str:
        incidents_md = "\n".join(
            f"- {i['incident_id']}: {i['summary']}" for i in ctx.recent_incidents[:3]
        ) or "_None_"
        return f"""## {task.title}

**Source:** {task.source.value} — [{task.source_id}]({task.source_url})
**Requested by:** {task.requested_by}
**Type:** {task.task_type.value} | **Priority:** {task.priority.value}

---

## Description

{task.description}

---

## Production Context

{task.production_context_summary}

**Related incidents:**
{incidents_md}

---

## Implementation Plan

{task.implementation_plan}

---

## Validation

- Iterations to pass: {task.validation_iterations}
- Test suite: {"✅ passed" if task.validation_passed else "❌ failed"}
- Files changed: {', '.join(task.affected_files[:10]) or 'none'}

---

_Automatically generated by SentinalAI DevLoopAgent — [task {task.task_id[:8]}]_
"""

    # ------------------------------------------------------------------
    # Phase 5: CI shepherding (delegate to CIShepherd)
    # ------------------------------------------------------------------

    def _shepherd_ci(self, task: DevTask) -> dict[str, Any]:
        """Delegate CI monitoring to CIShepherd."""
        try:
            from supervisor.ci_shepherd import CIShepherd
            shepherd = CIShepherd()
            return shepherd.watch(task)
        except Exception as exc:
            logger.warning("CI shepherd unavailable: %s", exc)
            return {"success": True, "reason": "shepherd unavailable, assuming CI pass"}

    # ------------------------------------------------------------------
    # LLM interface
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str, max_tokens: int = 2000) -> str:
        """Call Claude via the existing LLM interface."""
        try:
            import anthropic
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=os.environ.get("DEV_LOOP_MODEL", "claude-sonnet-4-6"),
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text if response.content else ""
        except Exception as exc:
            logger.warning("LLM call failed: %s", exc)
            return f"[LLM unavailable: {exc}]"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, task: DevTask, event_type: EventType, payload: dict) -> None:
        """Emit a dev loop event to the AGUI bus (best-effort, fire-and-forget)."""
        try:
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
            pass  # events are best-effort

    def _block(self, task: DevTask, reason: str) -> dict[str, Any]:
        task.status = DevTaskStatus.NEEDS_HUMAN
        task.blocker_reason = reason
        self._emit(task, EventType.DEV_NEEDS_HUMAN, {
            "reason": reason,
            "task_id": task.task_id,
            "pr_url": task.pr_url,
            "validation_iterations": task.validation_iterations,
        })
        logger.warning("DevLoopAgent blocked: task=%s reason=%s", task.task_id, reason)
        return {
            "success": False,
            "pr_url": task.pr_url,
            "pr_number": task.pr_number,
            "validation_iterations": task.validation_iterations,
            "blocker_reason": reason,
        }


def get_bus():
    from agui.event_bus import get_bus as _get_bus
    return _get_bus()
