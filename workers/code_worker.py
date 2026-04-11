"""Code Worker — AI-powered code diff analysis and fix generation.

This is the "principal engineer AI" that reads an actual code diff, cross-references
it with production error patterns from Splunk/APM, and:
  1. Identifies the exact lines causing the production issue
  2. Generates a concrete fix (patch + explanation)

Design principles (Karpathy-style):
- Feed the model MAXIMUM context: full diff + error logs + stack traces
- Chain-of-thought reasoning: model must show its work before the fix
- Confidence gate: only produce a fix if confidence >= threshold
- Two fix modes: "rollback" (fast, safe) and "code_fix" (permanent)
- Never guess — if evidence is insufficient, return confidence=0

Architecture:
    Agent -> CodeWorker -> LLM (diff analysis)
    Agent -> CodeWorker -> DevopsWorker.create_fix_pr (apply code fix)
    Agent -> CodeWorker -> DevopsWorker.rollback_deployment (apply rollback)

Usage:
    from workers.code_worker import CodeWorker

    worker = CodeWorker()
    analysis = worker.execute("analyze_diff", {
        "repo": "myorg/payment-service",
        "sha": "abc123",
        "diff": "...",
        "error_context": "NullPointerException at PaymentProcessor.java:42",
    })
    # -> {"culprit_file": "...", "culprit_line": 42, "reasoning": "...", "confidence": 87}
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from workers.base_worker import BaseWorker
from workers.mcp_client import McpGateway

logger = logging.getLogger("sentinalai.code_worker")

# Minimum confidence before producing a fix recommendation
_MIN_FIX_CONFIDENCE = int(os.environ.get("CODE_WORKER_MIN_CONFIDENCE", "60"))

# Maximum diff size in chars passed to the LLM (prevents token blow-up)
_MAX_DIFF_CHARS = int(os.environ.get("CODE_WORKER_MAX_DIFF_CHARS", "8000"))


class CodeWorker(BaseWorker):
    """Worker that performs AI-powered code diff analysis and fix generation."""

    worker_name = "code_worker"

    def __init__(self, gateway: McpGateway | None = None) -> None:
        super().__init__()
        self._gateway = gateway or McpGateway.get_instance()
        self.register("analyze_diff", self._analyze_diff)
        self.register("generate_fix", self._generate_fix)

    # ------------------------------------------------------------------ #
    # Action: analyze_diff
    # ------------------------------------------------------------------ #

    def _analyze_diff(self, params: dict) -> dict:
        """Analyze a code diff against production error context to find the bug.

        Params:
            repo:          org/repo string
            sha:           Commit SHA being analyzed
            diff:          The unified diff text (from get_commit_diff)
            error_context: Error messages, stack traces from Splunk/APM
            service:       Service name (for context)
            pr_number:     Optional PR number

        Returns:
            {
              "culprit_file":    "src/foo/Bar.java",
              "culprit_line":    42,
              "culprit_snippet": "...",
              "reasoning":       "The change on line 42 removes null check...",
              "confidence":      87,
              "analysis_source": "llm" | "pattern_match" | "stub",
            }
        """
        diff = params.get("diff", "")
        error_context = params.get("error_context", "")
        repo = params.get("repo", "")
        sha = params.get("sha", "")

        if not diff:
            return {"error": "diff is required", "confidence": 0}

        # Try LLM-powered analysis first
        from supervisor.llm import is_enabled as llm_enabled, converse
        if llm_enabled():
            return self._llm_analyze_diff(diff, error_context, repo, sha, params)

        # Fallback: pattern-based analysis (fast, less accurate)
        return self._pattern_analyze_diff(diff, error_context, repo, sha)

    def _llm_analyze_diff(
        self, diff: str, error_context: str, repo: str, sha: str, params: dict
    ) -> dict:
        """Use LLM to identify the causal change in the diff."""
        from supervisor.llm import converse

        # Truncate diff to prevent token limit blow-up
        truncated_diff = diff[:_MAX_DIFF_CHARS]
        if len(diff) > _MAX_DIFF_CHARS:
            truncated_diff += f"\n... [diff truncated at {_MAX_DIFF_CHARS} chars] ..."

        system_prompt = """\
You are a senior SRE and software engineer performing root cause analysis.
You have been given:
1. A code diff (the change that was deployed to production)
2. Error context from production logs/APM (what went wrong after the deploy)

Your job:
1. Read the diff carefully
2. Cross-reference each changed line with the error context
3. Identify the EXACT file and line number that caused the production issue
4. Explain your reasoning step by step
5. Assign a confidence score (0-100)

Rules:
- Only cite evidence that is actually present in the diff or error context
- If the error context mentions a specific file/line, check if the diff touched it
- If you cannot find a clear causal link, set confidence < 50
- Return ONLY valid JSON, no markdown fences

Output JSON schema:
{
  "culprit_file": "path/to/file.py or null",
  "culprit_line": 42,
  "culprit_snippet": "the problematic line of code",
  "reasoning": "step-by-step explanation",
  "confidence": 0-100,
  "analysis_source": "llm"
}"""

        user_message = f"""Repository: {repo}
Commit SHA: {sha}
Service: {params.get("service", "unknown")}
PR: {params.get("pr_number", "N/A")}

=== PRODUCTION ERROR CONTEXT ===
{error_context or "No error context provided"}

=== CODE DIFF ===
{truncated_diff}

Analyze the diff and identify what change caused the production error."""

        try:
            result = converse(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=0.0,
            )
            if result.get("error") or not result.get("text"):
                logger.warning("LLM diff analysis returned empty result, falling back to pattern match")
                return self._pattern_analyze_diff(diff, error_context, repo, sha)

            text = result["text"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].rstrip()

            parsed = json.loads(text)
            # Ensure required fields
            parsed.setdefault("confidence", 0)
            parsed.setdefault("culprit_file", None)
            parsed.setdefault("culprit_line", None)
            parsed.setdefault("culprit_snippet", "")
            parsed.setdefault("reasoning", "")
            parsed["analysis_source"] = "llm"
            logger.info(
                "LLM diff analysis: culprit=%s:%s confidence=%d",
                parsed.get("culprit_file"), parsed.get("culprit_line"), parsed.get("confidence"),
            )
            return parsed

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("LLM diff analysis parse error: %s", exc)
            return self._pattern_analyze_diff(diff, error_context, repo, sha)

    def _pattern_analyze_diff(
        self, diff: str, error_context: str, repo: str, sha: str
    ) -> dict:
        """Simple pattern matching fallback — extracts filenames from diff."""
        import re
        files: list[str] = re.findall(r"^(?:\+\+\+|---)\s+[ab]/(.+)$", diff, re.MULTILINE)
        unique_files = list(dict.fromkeys(f for f in files if not f.endswith("/dev/null")))
        culprit_file = unique_files[0] if unique_files else None

        # Try to find the error line number from error_context
        culprit_line: int | None = None
        if error_context:
            line_match = re.search(r":(\d+)\)", error_context)
            if line_match:
                culprit_line = int(line_match.group(1))

        confidence = 40 if culprit_file else 0
        return {
            "culprit_file": culprit_file,
            "culprit_line": culprit_line,
            "culprit_snippet": "",
            "reasoning": f"Pattern-based analysis identified {len(unique_files)} changed file(s). "
                         f"Manual review required.",
            "confidence": confidence,
            "analysis_source": "pattern_match",
        }

    # ------------------------------------------------------------------ #
    # Action: generate_fix
    # ------------------------------------------------------------------ #

    def _generate_fix(self, params: dict) -> dict:
        """Generate a concrete fix based on diff analysis.

        Params:
            analysis:       Result from analyze_diff
            diff:           The original unified diff
            error_context:  Error messages from production
            service:        Service name
            repo:           org/repo
            sha:            Commit SHA that caused the issue
            deployment_sha: The SHA currently deployed (for rollback)
            incident_id:    For traceability

        Returns:
            {
              "fix_type":          "rollback" | "code_fix" | "config_change",
              "fix_description":   "...",
              "immediate_action":  {"type": "rollback", "command": "..."},
              "permanent_action":  {"type": "pr", "title": "...", "body": "...", "patch": "..."},
              "confidence":        87,
              "risk_level":        "low" | "medium" | "high",
              "requires_approval": true,
            }
        """
        analysis = params.get("analysis", {})
        diff = params.get("diff", "")
        error_context = params.get("error_context", "")
        service = params.get("service", "unknown")
        repo = params.get("repo", "")
        sha = params.get("sha", "")
        incident_id = params.get("incident_id", "")

        confidence = analysis.get("confidence", 0)
        if confidence < _MIN_FIX_CONFIDENCE:
            logger.info(
                "generate_fix: confidence=%d < threshold=%d, skipping fix generation",
                confidence, _MIN_FIX_CONFIDENCE,
            )
            return {
                "fix_type": "none",
                "fix_description": f"Insufficient confidence ({confidence}%) to generate fix. "
                                   "Manual investigation required.",
                "confidence": confidence,
                "risk_level": "high",
                "requires_approval": True,
            }

        # Always include immediate rollback option
        immediate_action = {
            "type": "rollback",
            "command": f"kubectl rollout undo deployment/{service}",
            "description": f"Rollback {service} to previous deployment (revert commit {sha[:8]})",
        }

        # Try to generate permanent code fix via LLM
        from supervisor.llm import is_enabled as llm_enabled
        if llm_enabled() and analysis.get("culprit_file"):
            return self._llm_generate_fix(
                analysis, diff, error_context, service, repo, sha,
                immediate_action, incident_id,
            )

        # Fallback: rollback-only recommendation
        return {
            "fix_type": "rollback",
            "fix_description": (
                f"Rollback {service} to the previous deployment. "
                f"Commit {sha[:8]} likely introduced the issue in "
                f"{analysis.get('culprit_file', 'unknown file')}."
            ),
            "immediate_action": immediate_action,
            "permanent_action": None,
            "confidence": confidence,
            "risk_level": "low",
            "requires_approval": True,
            "incident_id": incident_id,
        }

    def _llm_generate_fix(
        self, analysis: dict, diff: str, error_context: str, service: str,
        repo: str, sha: str, immediate_action: dict, incident_id: str,
    ) -> dict:
        """Use LLM to generate a permanent code fix."""
        from supervisor.llm import converse

        truncated_diff = diff[:_MAX_DIFF_CHARS]

        system_prompt = """\
You are a senior software engineer generating a production hotfix.
You have:
1. A root cause analysis identifying the bug location
2. The code diff that introduced the bug
3. Production error context

Your job:
1. Write the minimal code fix (only change what's broken)
2. Write a PR title and description
3. Explain why this fix is correct

Rules:
- Minimal change principle: touch as few lines as possible
- The fix must directly address the root cause
- Include proper error handling
- Return ONLY valid JSON

Output JSON schema:
{
  "fix_type": "code_fix",
  "fix_description": "one-sentence description",
  "pr_title": "fix: ...",
  "pr_body": "markdown PR description with context",
  "patch": "unified diff of the fix",
  "confidence": 0-100,
  "risk_level": "low|medium|high"
}"""

        user_message = f"""Service: {service}
Repository: {repo}
Faulty commit: {sha}

Root cause analysis:
- File: {analysis.get('culprit_file')}
- Line: {analysis.get('culprit_line')}
- Snippet: {analysis.get('culprit_snippet')}
- Reasoning: {analysis.get('reasoning')}
- Confidence: {analysis.get('confidence')}%

Production error:
{error_context[:2000] if error_context else "Not provided"}

Original diff:
{truncated_diff}

Generate the minimal fix for this bug."""

        try:
            from supervisor.llm import converse
            result = converse(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=0.0,
            )

            if result.get("error") or not result.get("text"):
                raise ValueError("LLM returned empty fix")

            text = result["text"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].rstrip()

            fix = json.loads(text)
            fix.setdefault("fix_type", "code_fix")
            fix.setdefault("confidence", analysis.get("confidence", 0))
            fix.setdefault("risk_level", "medium")
            fix["requires_approval"] = True
            fix["immediate_action"] = immediate_action
            fix["incident_id"] = incident_id
            logger.info(
                "LLM fix generated: type=%s confidence=%d risk=%s",
                fix.get("fix_type"), fix.get("confidence"), fix.get("risk_level"),
            )
            return fix

        except Exception as exc:
            logger.warning("LLM fix generation failed: %s — falling back to rollback", exc)
            return {
                "fix_type": "rollback",
                "fix_description": f"LLM fix generation failed. Rollback {service} as safe option.",
                "immediate_action": immediate_action,
                "permanent_action": None,
                "confidence": analysis.get("confidence", 0),
                "risk_level": "low",
                "requires_approval": True,
                "incident_id": incident_id,
            }
