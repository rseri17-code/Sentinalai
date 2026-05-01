"""Dev task intake — Slack, Linear, and GitHub Issue webhooks.

Routes:
  POST /api/v1/dev/webhooks/slack   → Slack slash command or mention
  POST /api/v1/dev/webhooks/linear  → Linear issue webhook
  POST /api/v1/dev/webhooks/github  → GitHub Issues webhook
  POST /api/v1/dev/tasks            → Manual dev task submission

All endpoints:
  1. Parse and normalize to DevTask
  2. Load production context in background (KG, incident history, services)
  3. Dispatch DevLoopAgent asynchronously
  4. Return 202 Accepted with task_id and WebSocket URL

Signature validation:
  SLACK_SIGNING_SECRET    — verifies X-Slack-Signature header
  LINEAR_WEBHOOK_SECRET   — verifies Linear-Signature header
  GITHUB_WEBHOOK_SECRET   — verifies X-Hub-Signature-256 header
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from agui.schemas.dev_task import (
    DevTask, DevTaskSource, DevTaskType, DevTaskPriority, DevTaskStatus,
)
from agui.schemas.events import AGUIEvent, EventType
from agui.event_bus import get_bus

logger = logging.getLogger("sentinalai.dev_intake")

router = APIRouter(tags=["dev-loop"])

_SLACK_SECRET  = os.environ.get("SLACK_SIGNING_SECRET", "")
_LINEAR_SECRET = os.environ.get("LINEAR_WEBHOOK_SECRET", "")
_GITHUB_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

# In-memory task store (replace with DynamoDB in production)
_TASKS: dict[str, DevTask] = {}


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------

async def _verify_slack(request: Request) -> None:
    if not _SLACK_SECRET:
        return
    ts = request.headers.get("X-Slack-Request-Timestamp", "")
    sig = request.headers.get("X-Slack-Signature", "")
    if abs(time.time() - float(ts or 0)) > 300:
        raise HTTPException(status_code=401, detail="Slack timestamp too old")
    body = await request.body()
    base = f"v0:{ts}:{body.decode()}"
    expected = "v0=" + hmac.new(_SLACK_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(status_code=401, detail="Slack signature mismatch")


async def _verify_hmac(request: Request, secret: str, header: str) -> None:
    if not secret:
        return
    sig = request.headers.get(header, "")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.removeprefix("sha256=")):
        raise HTTPException(status_code=401, detail=f"Signature mismatch ({header})")


# ---------------------------------------------------------------------------
# Task dispatch
# ---------------------------------------------------------------------------

async def _dispatch_task(task: DevTask) -> dict[str, Any]:
    """Persist task and launch the dev loop agent."""
    _TASKS[task.task_id] = task
    asyncio.create_task(_run_dev_loop(task))

    await get_bus().publish(AGUIEvent(
        event_type=EventType.DEV_TASK_CREATED,
        investigation_id=task.task_id,
        incident_id=task.source_id or task.task_id,
        payload={
            "task_id": task.task_id,
            "title": task.title,
            "source": task.source.value,
            "priority": task.priority.value,
            "task_type": task.task_type.value,
            "affected_services": task.affected_services,
        },
    ))

    logger.info(
        "Dev task accepted: task_id=%s source=%s title=%r",
        task.task_id, task.source.value, task.title[:80],
    )
    return {
        "status": "accepted",
        "task_id": task.task_id,
        "source": task.source.value,
        "ws_url": f"/ws/dev/{task.task_id}",
        "title": task.title,
    }


async def _run_dev_loop(task: DevTask) -> None:
    """Run the DevLoopAgent in a thread pool."""
    loop = asyncio.get_event_loop()
    try:
        def _run():
            from supervisor.dev_loop_agent import DevLoopAgent
            agent = DevLoopAgent()
            return agent.run(task)

        result = await loop.run_in_executor(None, _run)
        task = _TASKS.get(task.task_id, task)
        task.status = DevTaskStatus.COMPLETED if result.get("success") else DevTaskStatus.FAILED
        task.pr_url = result.get("pr_url", "")
        task.pr_number = result.get("pr_number")
        _TASKS[task.task_id] = task
    except Exception as exc:
        logger.error("Dev loop failed for task %s: %s", task.task_id, exc)
        if task.task_id in _TASKS:
            _TASKS[task.task_id].status = DevTaskStatus.FAILED
            _TASKS[task.task_id].blocker_reason = str(exc)


# ---------------------------------------------------------------------------
# Slack webhook
# ---------------------------------------------------------------------------

@router.post("/api/v1/dev/webhooks/slack", status_code=status.HTTP_202_ACCEPTED)
async def slack_webhook(request: Request):
    """Accept Slack slash command or app mention and create a dev task.

    Supported Slack payloads:
    - Slash command: /sentinal build <description>
    - App mention: @sentinal fix <description>
    - Slash command payload via URL-encoded form

    The task description is extracted from the Slack text field.
    """
    await _verify_slack(request)
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        payload = await request.json()
    else:
        body = await request.body()
        from urllib.parse import parse_qs
        parsed = parse_qs(body.decode())
        payload = {k: v[0] for k, v in parsed.items()}

    # Slack slash command format
    text = payload.get("text", "") or payload.get("event", {}).get("text", "")
    user = (
        payload.get("user_id")
        or payload.get("user_name")
        or payload.get("event", {}).get("user", "unknown")
    )
    channel = payload.get("channel_id", "") or payload.get("channel", "")
    ts = payload.get("event", {}).get("ts", "") or payload.get("trigger_id", "")

    # Remove bot mention prefix if present
    import re
    text = re.sub(r"^<@[A-Z0-9]+>\s*", "", text).strip()

    if not text:
        return {"response_type": "ephemeral", "text": "Usage: /sentinal <description of what to build or fix>"}

    # Infer task type from trigger words
    task_type = _infer_task_type(text)
    priority = _infer_priority(text)

    task = DevTask(
        source=DevTaskSource.SLACK,
        source_id=ts,
        source_url=f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}",
        requested_by=user,
        title=text[:120],
        description=text,
        task_type=task_type,
        priority=priority,
    )
    return await _dispatch_task(task)


# ---------------------------------------------------------------------------
# Linear webhook
# ---------------------------------------------------------------------------

_LINEAR_TRIGGER_STATES = {"todo", "backlog", "in_progress", "unstarted"}


@router.post("/api/v1/dev/webhooks/linear", status_code=status.HTTP_202_ACCEPTED)
async def linear_webhook(request: Request):
    """Accept Linear issue webhook and create a dev task.

    Triggers on:
    - Issue created with label 'agent' or 'sentinal'
    - Issue moved to 'In Progress' with sentinal assignee

    Linear webhook payload: {"action": "create"|"update", "data": {...}, "type": "Issue"}
    """
    await _verify_hmac(request, _LINEAR_SECRET, "Linear-Signature")
    payload = await request.json()

    action = payload.get("action", "")
    obj_type = payload.get("type", "")
    data = payload.get("data", {})

    if obj_type != "Issue":
        return {"status": "ok", "note": "non-issue event ignored"}

    # Only trigger on create/update with agent label
    labels = [l.get("name", "").lower() for l in data.get("labels", [])]
    if not any(l in ("agent", "sentinal", "sentinalai", "auto") for l in labels):
        if action not in ("create",):
            return {"status": "ok", "note": "no agent label, skipped"}

    state_name = (data.get("state") or {}).get("name", "").lower()
    if state_name in ("done", "cancelled", "duplicate"):
        return {"status": "ok", "note": f"terminal state '{state_name}' ignored"}

    title = data.get("title", "")
    description = data.get("description", "") or title
    priority_map = {0: "medium", 1: "urgent", 2: "high", 3: "medium", 4: "low"}
    priority_raw = priority_map.get(data.get("priority", 3), "medium")

    task = DevTask(
        source=DevTaskSource.LINEAR,
        source_id=data.get("id", ""),
        source_url=data.get("url", ""),
        requested_by=(data.get("creator") or {}).get("email", "unknown"),
        title=title[:120],
        description=description,
        task_type=_infer_task_type(title + " " + description),
        priority=DevTaskPriority(priority_raw),
    )
    return await _dispatch_task(task)


# ---------------------------------------------------------------------------
# GitHub Issue webhook
# ---------------------------------------------------------------------------

@router.post("/api/v1/dev/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request):
    """Accept GitHub Issues/PR webhook and create a dev task.

    Triggers on:
    - Issue opened/labeled with 'agent' or 'sentinal' label
    - PR review comment requesting changes (for review_responder)

    GitHub sends X-GitHub-Event header to identify event type.
    """
    await _verify_hmac(request, _GITHUB_SECRET, "X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()

    if event_type == "issues":
        action = payload.get("action", "")
        if action not in ("opened", "labeled"):
            return {"status": "ok", "note": f"action '{action}' ignored"}

        issue = payload.get("issue", {})
        labels = [l.get("name", "").lower() for l in issue.get("labels", [])]
        if not any(l in ("agent", "sentinal", "sentinalai", "auto") for l in labels):
            return {"status": "ok", "note": "no agent label"}

        task = DevTask(
            source=DevTaskSource.GITHUB_ISSUE,
            source_id=str(issue.get("number", "")),
            source_url=issue.get("html_url", ""),
            requested_by=(issue.get("user") or {}).get("login", "unknown"),
            title=issue.get("title", "")[:120],
            description=issue.get("body", "") or issue.get("title", ""),
            task_type=_infer_task_type(issue.get("title", "") + " " + (issue.get("body") or "")),
            priority=_infer_priority(issue.get("title", "") + " " + (issue.get("body") or "")),
        )
        return await _dispatch_task(task)

    if event_type == "pull_request_review":
        # Route review events to the review responder
        action = payload.get("action", "")
        if action != "submitted":
            return {"status": "ok"}
        review = payload.get("review", {})
        if review.get("state") != "changes_requested":
            return {"status": "ok", "note": "non-blocking review"}

        pr = payload.get("pull_request", {})
        asyncio.create_task(_handle_review_event(pr, review, payload))
        return {"status": "ok", "note": "review dispatched to responder"}

    return {"status": "ok", "note": f"event '{event_type}' not handled"}


async def _handle_review_event(pr: dict, review: dict, payload: dict) -> None:
    loop = asyncio.get_event_loop()
    try:
        def _run():
            from supervisor.review_responder import ReviewResponder
            responder = ReviewResponder()
            return responder.handle_review(pr, review, payload)
        await loop.run_in_executor(None, _run)
    except Exception as exc:
        logger.error("Review responder failed: %s", exc)


# ---------------------------------------------------------------------------
# Manual dev task
# ---------------------------------------------------------------------------

class ManualDevTaskRequest(BaseModel):
    title: str
    description: str = ""
    task_type: str = "feature"
    priority: str = "medium"
    affected_services: list[str] = []
    requested_by: str = "manual"


@router.post("/api/v1/dev/tasks", status_code=status.HTTP_202_ACCEPTED)
async def submit_dev_task(req: ManualDevTaskRequest):
    """Submit a dev task manually — useful for CI/CD pipeline integration."""
    try:
        task_type = DevTaskType(req.task_type)
        priority = DevTaskPriority(req.priority)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    task = DevTask(
        source=DevTaskSource.MANUAL,
        requested_by=req.requested_by,
        title=req.title[:120],
        description=req.description or req.title,
        task_type=task_type,
        priority=priority,
        affected_services=req.affected_services,
    )
    return await _dispatch_task(task)


# ---------------------------------------------------------------------------
# Task state endpoints
# ---------------------------------------------------------------------------

@router.get("/api/v1/dev/tasks/{task_id}")
async def get_dev_task(task_id: str):
    """Get current state of a dev task."""
    task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.model_dump()


@router.get("/api/v1/dev/tasks")
async def list_dev_tasks(status: str = "", limit: int = 20):
    """List recent dev tasks, optionally filtered by status."""
    tasks = list(_TASKS.values())
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    tasks.sort(key=lambda t: t.started_at or "", reverse=True)
    return {"tasks": [t.model_dump() for t in tasks[:limit]], "total": len(tasks)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_task_type(text: str) -> DevTaskType:
    t = text.lower()
    if any(w in t for w in ("fix", "bug", "broken", "error", "crash", "failing")):
        return DevTaskType.BUG_FIX
    if any(w in t for w in ("refactor", "clean", "simplif", "restructure")):
        return DevTaskType.REFACTOR
    if any(w in t for w in ("perf", "slow", "latency", "optimis", "optimiz", "speed")):
        return DevTaskType.PERFORMANCE
    if any(w in t for w in ("security", "vuln", "cve", "auth", "injection", "xss")):
        return DevTaskType.SECURITY
    if any(w in t for w in ("upgrade", "bump", "dependency", "package", "version")):
        return DevTaskType.DEPENDENCY
    if any(w in t for w in ("test", "coverage", "spec", "assert")):
        return DevTaskType.TEST
    if any(w in t for w in ("doc", "readme", "comment", "docstring")):
        return DevTaskType.DOCS
    return DevTaskType.FEATURE


def _infer_priority(text: str) -> DevTaskPriority:
    t = text.lower()
    if any(w in t for w in ("urgent", "asap", "critical", "p0", "sev1", "outage", "down")):
        return DevTaskPriority.URGENT
    if any(w in t for w in ("high", "important", "p1", "sev2")):
        return DevTaskPriority.HIGH
    if any(w in t for w in ("low", "minor", "p3", "nice to have", "backlog")):
        return DevTaskPriority.LOW
    return DevTaskPriority.MEDIUM
