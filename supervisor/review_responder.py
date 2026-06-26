"""Review Responder — automatically addresses PR review comments.

When a PR reviewer requests changes, ReviewResponder:
  1. Parses all reviewer comments (inline + general)
  2. Groups comments by file for efficient context loading
  3. Asks Claude to generate responses and code fixes
  4. Applies fixes and pushes a follow-up commit
  5. Posts acknowledgement replies to each comment via GitHub MCP
  6. Re-requests review

This closes the human-review feedback loop without engineer intervention
for straightforward review feedback (style fixes, minor logic changes,
missing tests, documentation gaps).

For substantive architectural disagreements, the responder posts a
transparent explanation and flags NEEDS_HUMAN.

Configuration:
  REVIEW_RESPONDER_ENABLED   — on/off (default: true)
  REVIEW_MAX_COMMENTS        — max comments to address per PR (default: 20)
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from sentinel_core.models.dev_task import DevTask, DevTaskStatus, ReviewComment
from sentinel_core.models.events import AGUIEvent, EventType

logger = logging.getLogger("sentinalai.review_responder")

REVIEW_RESPONDER_ENABLED = os.environ.get(
    "REVIEW_RESPONDER_ENABLED", "true"
).lower() in ("1", "true", "yes")
REVIEW_MAX_COMMENTS = int(os.environ.get("REVIEW_MAX_COMMENTS", "20"))

REPO_ROOT = os.environ.get("REPO_ROOT", os.path.join(os.path.dirname(__file__), ".."))


class ReviewResponder:
    """Addresses PR review comments autonomously."""

    def handle_review(
        self,
        pr: dict[str, Any],
        review: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle an incoming review event from GitHub webhook.

        Args:
            pr:      GitHub PR object (number, head, base, title, etc.)
            review:  GitHub review object (state, body, user, etc.)
            payload: Full webhook payload

        Returns: {"success": bool, "comments_addressed": int, "blocker": str}
        """
        if not REVIEW_RESPONDER_ENABLED:
            return {"success": False, "blocker": "ReviewResponder disabled"}

        pr_number = pr.get("number")
        pr_branch = pr.get("head", {}).get("ref", "")
        reviewer = review.get("user", {}).get("login", "unknown")

        logger.info(
            "ReviewResponder: PR #%s reviewer=%s state=%s",
            pr_number, reviewer, review.get("state"),
        )

        # Fetch inline comments for this review
        comments = self._fetch_review_comments(pr_number, review.get("id"))
        if not comments:
            # General review body only
            general_body = review.get("body", "")
            if general_body:
                comments = [{"body": general_body, "path": "", "line": 0, "id": "general"}]

        if not comments:
            return {"success": True, "comments_addressed": 0}

        comments = comments[:REVIEW_MAX_COMMENTS]

        # Build a task context for the responder
        task = DevTask(
            title=pr.get("title", ""),
            description=pr.get("body", ""),
            pr_number=pr_number,
            pr_branch=pr_branch,
            pr_url=pr.get("html_url", ""),
        )

        # Address all comments
        addressed = 0
        code_changes: dict[str, str] = {}   # file_path → new content
        comment_replies: list[tuple[str, str]] = []   # (comment_id, reply_text)

        for comment in comments:
            result = self._address_comment(comment, pr, task)
            if result.get("needs_human"):
                self._post_comment_reply(
                    pr_number,
                    comment.get("id"),
                    f"👋 This feedback requires human review: {result.get('reason', '')}",
                )
                self._emit_needs_human(task, result.get("reason", ""))
                continue

            if result.get("code_fix"):
                for path, content in result["code_fix"].items():
                    code_changes[path] = content

            if result.get("reply"):
                comment_replies.append((comment.get("id"), result["reply"]))

            addressed += 1

        # Apply all code changes in one commit
        if code_changes:
            pushed = self._apply_and_push(task, code_changes, reviewer)
            if not pushed:
                return {
                    "success": False,
                    "comments_addressed": addressed,
                    "blocker": "Failed to push review fixes",
                }

        # Post replies after pushing
        for comment_id, reply in comment_replies:
            self._post_comment_reply(pr_number, comment_id, reply)

        # Re-request review
        if code_changes:
            self._request_re_review(pr_number, reviewer)

        self._emit_responded(task, addressed, bool(code_changes))

        logger.info(
            "ReviewResponder: addressed %d comments on PR #%s, pushed=%s",
            addressed, pr_number, bool(code_changes),
        )
        return {"success": True, "comments_addressed": addressed, "blocker": ""}

    # ------------------------------------------------------------------
    # Comment addressing
    # ------------------------------------------------------------------

    def _address_comment(
        self,
        comment: dict[str, Any],
        pr: dict[str, Any],
        task: DevTask,
    ) -> dict[str, Any]:
        """Decide how to handle one review comment.

        Returns:
          {"code_fix": {path: content}, "reply": str, "needs_human": bool, "reason": str}
        """
        body = comment.get("body", "")
        file_path = comment.get("path", "")
        line = comment.get("line") or comment.get("original_line") or 0

        # Classify the comment
        classification = self._classify_comment(body)

        if classification == "architectural":
            return {
                "needs_human": True,
                "reason": f"Architectural feedback on {file_path}:{line} requires engineer decision",
            }

        # Load the current file content for context
        file_content = ""
        if file_path:
            abs_path = os.path.join(REPO_ROOT, file_path)
            try:
                with open(abs_path) as f:
                    file_content = f.read()
            except Exception:
                pass

        prompt = self._build_response_prompt(body, file_path, line, file_content, pr, classification)
        response = self._call_llm(prompt)

        # Parse code fix from response
        code_fix: dict[str, str] = {}
        pattern = r"```(?:[\w.]+\n)?([\w./\-]+\.[a-zA-Z]+)\n(.*?)```"
        for path, content in re.findall(pattern, response, re.DOTALL):
            code_fix[path.strip()] = content.strip()

        # Extract reply text (everything outside code blocks)
        reply = re.sub(r"```.*?```", "", response, flags=re.DOTALL).strip()
        reply = reply[:500] if reply else f"Applied fix for: {body[:100]}"

        return {"code_fix": code_fix if code_fix else {}, "reply": reply, "needs_human": False}

    def _classify_comment(self, body: str) -> str:
        """Classify a review comment by effort/type.

        Returns: "style" | "logic" | "test" | "docs" | "architectural"
        """
        b = body.lower()
        if any(w in b for w in ("architecture", "design decision", "rethink", "fundamentally", "refactor the whole")):
            return "architectural"
        if any(w in b for w in ("test", "coverage", "assert", "mock", "fixture")):
            return "test"
        if any(w in b for w in ("doc", "comment", "docstring", "readme", "type hint")):
            return "docs"
        if any(w in b for w in ("style", "format", "indent", "naming", "whitespace", "lint")):
            return "style"
        return "logic"

    def _build_response_prompt(
        self,
        comment_body: str,
        file_path: str,
        line: int,
        file_content: str,
        pr: dict[str, Any],
        classification: str,
    ) -> str:
        context = ""
        if file_content and file_path:
            lines = file_content.split("\n")
            start = max(0, line - 10)
            end = min(len(lines), line + 10)
            context = "\n".join(f"{i+1}: {l}" for i, l in enumerate(lines[start:end], start=start))

        return f"""You are responding to a code review comment on a pull request.

PR: {pr.get('title', '')}
File: {file_path}:{line}
Comment type: {classification}

Reviewer comment:
"{comment_body}"

Current file content (around line {line}):
```
{context}
```

Instructions:
1. If a code change is needed: provide the complete corrected file content as:
   ```{file_path}
   <full corrected file content>
   ```
2. Write a brief, professional reply acknowledging the feedback (1-2 sentences).
3. For style/docs comments: apply exactly what was requested.
4. For logic comments: reason carefully, apply the fix only if it's clearly correct.
5. Never argue with the reviewer — if uncertain, apply the change and explain.

Do not explain your reasoning beyond the reply. Output: code block (if needed) + reply."""

    # ------------------------------------------------------------------
    # GitHub MCP calls
    # ------------------------------------------------------------------

    def _fetch_review_comments(self, pr_number: int, review_id: Any) -> list[dict]:
        """Fetch inline review comments from GitHub."""
        try:
            from workers.mcp_client import MCPClient
            client = MCPClient()
            result = client.call(
                "github.get_pr_details",
                {"pr_number": pr_number, "include_review_comments": True},
            )
            comments = result.get("review_comments", result.get("comments", []))
            # Filter to this review if review_id provided
            if review_id:
                comments = [c for c in comments if c.get("pull_request_review_id") == review_id]
            return comments
        except Exception as exc:
            logger.debug("Fetch review comments failed: %s", exc)
            return []

    def _post_comment_reply(self, pr_number: int, comment_id: Any, reply: str) -> None:
        """Post a reply to a review comment."""
        if not comment_id or comment_id == "general":
            return
        try:
            from workers.mcp_client import MCPClient
            MCPClient().call(
                "github.reply_to_review_comment",
                {"pr_number": pr_number, "comment_id": comment_id, "body": reply},
            )
        except Exception as exc:
            logger.debug("Post reply failed: %s", exc)

    def _request_re_review(self, pr_number: int, reviewer: str) -> None:
        """Re-request review from the original reviewer."""
        try:
            from workers.mcp_client import MCPClient
            MCPClient().call(
                "github.request_reviewers",
                {"pr_number": pr_number, "reviewers": [reviewer]},
            )
        except Exception as exc:
            logger.debug("Re-request review failed: %s", exc)

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def _apply_and_push(
        self,
        task: DevTask,
        code_changes: dict[str, str],
        reviewer: str,
    ) -> bool:
        """Write fixed files and push a fixup commit."""
        for file_path, content in code_changes.items():
            abs_path = os.path.join(REPO_ROOT, file_path)
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w") as f:
                    f.write(content + "\n")
            except Exception as exc:
                logger.warning("Review fix write failed for %s: %s", file_path, exc)
                return False

        try:
            subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m",
                 f"fix: address review feedback from {reviewer} [sentinal-auto]"],
                cwd=REPO_ROOT, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "push", "origin", task.pr_branch],
                cwd=REPO_ROOT, check=True, capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as exc:
            logger.warning("Review fix push failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------

    def _call_llm(self, prompt: str) -> str:
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
            logger.warning("ReviewResponder LLM call failed: %s", exc)
            return f"[LLM unavailable: {exc}]"

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _emit_responded(self, task: DevTask, count: int, pushed: bool) -> None:
        self._emit(task, EventType.DEV_REVIEW_RESPONDED, {
            "comments_addressed": count,
            "code_pushed": pushed,
            "pr_url": task.pr_url,
        })

    def _emit_needs_human(self, task: DevTask, reason: str) -> None:
        self._emit(task, EventType.DEV_NEEDS_HUMAN, {
            "reason": reason,
            "pr_url": task.pr_url,
        })

    def _emit(self, task: DevTask, event_type: EventType, payload: dict) -> None:
        try:
            import asyncio
            from agui.event_bus import get_bus
            event = AGUIEvent(
                event_type=event_type,
                investigation_id=task.task_id,
                incident_id=task.source_id if hasattr(task, "source_id") else task.task_id,
                payload=payload,
            )
            loop = asyncio.new_event_loop()
            loop.run_until_complete(get_bus().publish(event))
            loop.close()
        except Exception:
            pass
