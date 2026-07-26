"""Investigation execution + trace capture.

Drives the UNMODIFIED ``SentinalAISupervisor.investigate(incident_id)`` engine
against one scenario's rendered telemetry and captures the complete observable
trace. Nothing here edits the engine, planner, runtime, replay, or scoring — the
engine is treated as a black box and driven through its real MCP boundary.

Evaluated configuration (fixed for every scenario, recorded in the report):

* ``LLM_ENABLED=false`` — the deterministic reasoning core, without the LLM
  refinement layer (a live LLM is non-deterministic and needs network/credentials,
  so it is out of scope for a hermetic benchmark; the repo's own determinism suite
  runs this way). EB-2 therefore measures the engine's *deterministic core*.
* ``CALIBRATION_ENABLED=false``, ``RECURRENCE_ENABLED=false``,
  ``KNOWLEDGE_GRAPH_ENABLED=false`` and every learning-state path redirected to an
  empty temp dir — so each incident is investigated from ITS OWN evidence, with no
  cross-incident memory. This makes results deterministic, order-independent, and
  reproducible (independent of mutable committed ``eval/`` state).
* The engine's reasoning stack is enabled (hypothesis engine, causal localization,
  decision intelligence, validation) so the investigation *process* is observable.
  These are the engine's own features, toggled by their own flags — no code is
  changed and no hidden knowledge is exposed.

Determinism is achieved two ways, both necessary: (1) each investigation runs in a
fresh subprocess with empty learning state (no in-memory singletons or background
writes leak between scenarios); (2) concurrency-ordered trace fields (the engine
dispatches collection workers on a thread pool) are canonicalized. Verified by the
determinism test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from enterprisebench.pipeline.bench_source import BenchMCPSource
from enterprisebench.pipeline.render import render

# Every learning/persisted-state path the engine may read or write. Redirected to
# a fresh empty dir so a scenario neither learns from another nor pollutes the repo.
_STATE_PATH_VARS = (
    "EXPERIENCE_STORE_PATH", "KNOWLEDGE_GRAPH_PATH", "CALIBRATION_MAP_PATH",
    "ADAPTIVE_THRESHOLDS_PATH", "EVOLVED_STRATEGY_PATH", "GAP_AGGREGATOR_PATH",
    "INCIDENT_GIT_INDEX_PATH", "PATTERN_REGISTRY_PATH", "PATTERN_SIGNATURES_PATH",
    "RECURRENCE_INDEX_PATH", "CO_FAILURE_INDEX_PATH", "CASCADE_TRACKER_PATH",
    "BLAST_RADIUS_HISTORY_PATH", "RESOLUTION_OUTCOMES_PATH", "SERVICE_PROFILES_PATH",
    "RETRIEVAL_CACHE_PATH", "RETRIEVAL_TELEMETRY_PATH", "MEMORY_RECORD_STORE_PATH",
    "ARTIFACT_STORE_PATH", "OPS_DB_PATH", "WORKFLOW_DB_PATH", "IMPROVEMENT_REPORT_PATH",
    "EPISODIC_MEMORY_PATH",   # repo-anchored (dirname(__file__)); needs explicit override
)
# Flags fixing the evaluated configuration (see module docstring).
_CONFIG_FLAGS_OFF = ("LLM_ENABLED", "CALIBRATION_ENABLED", "RECURRENCE_ENABLED",
                     "KNOWLEDGE_GRAPH_ENABLED", "MCP_DEDUP_ENABLED",
                     "INTELLIGENCE_ENABLED")  # async learning/background writer
_CONFIG_FLAGS_ON = ("HYPOTHESIS_ENGINE_ENABLED", "CAUSAL_INVESTIGATION_ENABLED",
                    "DECISION_INTELLIGENCE_ENABLED", "VALIDATION_ENGINE_ENABLED")

# The exact configuration string recorded in the report for reproducibility.
EVALUATED_CONFIG = {
    "off": list(_CONFIG_FLAGS_OFF),
    "on": list(_CONFIG_FLAGS_ON),
    "note": "deterministic core: LLM refinement excluded; cross-incident learning "
            "neutralized; engine reasoning stack enabled; per-incident isolation.",
}


def configure_deterministic_offline_env(state_dir: str, *, force: bool = True) -> None:
    """Set the fixed evaluated configuration. ``force`` overrides any inherited
    value (used inside the isolated worker); the in-process path uses setdefault."""
    setter = (lambda k, v: os.environ.__setitem__(k, v)) if force else os.environ.setdefault
    for f in _CONFIG_FLAGS_OFF:
        setter(f, "false")
    for f in _CONFIG_FLAGS_ON:
        setter(f, "true")
    for var in _STATE_PATH_VARS:
        setter(var, os.path.join(state_dir, var.lower() + ".state"))
    # Redirect side-effect receipt/wiki writes into the sandbox.
    setter("AGUI_LOCAL_RECEIPT_DIR", os.path.join(state_dir, "receipts"))
    setter("SENTINEL_WIKI_DIR", os.path.join(state_dir, "wiki"))


def _run_in_process(task: Mapping[str, Any]) -> dict[str, Any]:
    """Run one investigation in THIS process (fresh state assumed). Used by the
    isolated worker; not called directly by the pipeline."""
    rendered = render(task)
    source = BenchMCPSource(rendered)
    from supervisor.agent import SentinalAISupervisor
    supervisor = SentinalAISupervisor(gateway=source)
    result = supervisor.investigate(str(task.get("task_id", "")))
    return capture_trace(task, result, source)


def run_investigation(task: Mapping[str, Any], *,
                      timeout: int = 180) -> dict[str, Any]:
    """Render telemetry, drive the unmodified engine in an ISOLATED subprocess,
    return the canonical trace.

    Reads only the PUBLIC ``task`` fields (incident + telemetry). The subprocess
    starts with empty learning state, so runs are deterministic and
    order-independent. Raises ``InvestigationError`` on subprocess failure.
    """
    with tempfile.TemporaryDirectory(prefix="eb2-run-") as tmp:
        task_path = os.path.join(tmp, "task.json")
        out_path = os.path.join(tmp, "trace.json")
        with open(task_path, "w") as f:
            json.dump(dict(task), f)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("AGUI_AUTH_REQUIRED", "false")
        # cwd=tmp so any relative "eval/..." state path resolves under the temp
        # dir (not the repo). Repo-anchored writers are handled by the env paths.
        proc = subprocess.run(
            [sys.executable, "-m", "enterprisebench.pipeline._isolated_worker",
             task_path, out_path],
            env=env, cwd=tmp, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not os.path.exists(out_path):
            raise InvestigationError(
                f"isolated investigation failed (rc={proc.returncode}): "
                f"{proc.stderr[-800:]}")
        with open(out_path) as f:
            return json.load(f)


class InvestigationError(RuntimeError):
    """Raised when the isolated investigation subprocess fails."""


def _canonical_queries(query_log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize the MCP query stream: the engine dispatches collection workers
    concurrently, so intra-run ORDER is nondeterministic — the multiset is not.
    Sort deterministically and renumber; drop the volatile concurrent ``seq``."""
    ordered = sorted(query_log, key=lambda q: (q.get("server", ""),
                                               q.get("action", ""),
                                               q.get("query", ""),
                                               q.get("mcp_tool_name", "")))
    return [{k: v for k, v in q.items() if k != "seq"} for q in ordered]


def capture_trace(task: Mapping[str, Any], result: Mapping[str, Any],
                  source: BenchMCPSource) -> dict[str, Any]:
    """Normalize the observable investigation into a canonical (deterministic)
    trace. Reads only what the black box exposed; sorts concurrency-ordered fields.
    """
    from sentinel_core.eic.adapter import sentinelai_submission

    submission = sentinelai_submission(
        result, task_id=str(task.get("task_id", "")),
        replay_hash=str(result.get("corpus_stamp", "")))
    # Canonicalize set-semantic fields (order is concurrency noise).
    submission["hypotheses"] = sorted(submission.get("hypotheses", []))
    submission["ruled_out"] = sorted(submission.get("ruled_out", []))

    receipts = result.get("receipts") or []
    return {
        "task_id": str(task.get("task_id", "")),
        "root_cause": str(result.get("root_cause", "")),
        "confidence": int(result.get("confidence", 0) or 0),
        "localized_service": submission.get("localized_service", ""),
        "reasoning": str(result.get("reasoning", "")),
        "recommendation": str(result.get("remediation")
                              or result.get("proposed_fix") or ""),
        "hypotheses": submission["hypotheses"],
        "ruled_out": submission["ruled_out"],
        "evidence_used": submission.get("evidence_used", []),
        "decisive_evidence": sorted(submission.get("decisive_evidence", [])),
        "mcp_queries": _canonical_queries(source.query_log),
        "servers_queried": sorted(source.servers_queried()),
        "rendered_channels_served": source.rendered_channels_served(),
        "render_provenance": source.provenance,
        "receipt_count": len(receipts) if isinstance(receipts, list) else 0,
        "winner_hypothesis": str(result.get("winner_hypothesis", "")),
        "hypothesis_count": int(result.get("hypothesis_count", 0) or 0),
        "replay_available": bool(result.get("corpus_stamp")),
        "submission": submission,
    }


__all__ = ["run_investigation", "capture_trace", "InvestigationError",
           "configure_deterministic_offline_env", "EVALUATED_CONFIG"]
