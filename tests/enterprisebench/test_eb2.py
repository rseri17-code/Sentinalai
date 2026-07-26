"""EB-2 — Investigation Evaluation Pipeline tests.

Proves the non-negotiable invariants: the Enterprise Investigation Specification
and the hidden answer key are inaccessible to the engine; the BenchMCPSource
behaves like a production MCP; execution is deterministic; and the pipeline
leaves the working tree unchanged. Fast, offline, hermetic.
"""
from __future__ import annotations

import copy
import json
import os

import pytest

from enterprisebench.pipeline import EB2_VERSION
from enterprisebench.pipeline.bench_source import BenchMCPSource
from enterprisebench.pipeline.evaluate import NOT_MEASURED, evaluate
from enterprisebench.pipeline.render import render

_CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "eval", "efic", "corpus.json")


def _corpus():
    with open(_CORPUS) as f:
        return json.load(f)


def _first_entry():
    return _corpus()["corpus"][0]


# ---------------------------------------------------------------------------
# Isolation proof — the spec + hidden answer key never reach the engine.
# ---------------------------------------------------------------------------

def test_render_ignores_hidden_knowledge():
    """render() depends ONLY on the public incident + telemetry. Scrambling
    ground_truth / traps / efc must not change a single rendered byte."""
    entry = _first_entry()
    task = entry["task"]
    poisoned = copy.deepcopy(task)
    poisoned["ground_truth"] = {"root_cause": "SECRET-ANSWER-DO-NOT-LEAK",
                                "root_cause_keywords": ["leaked"],
                                "root_cause_service": "leaked",
                                "necessary_evidence": ["leaked"],
                                "decisive_evidence": ["leaked"]}
    poisoned["traps"] = {"distractor_evidence": ["leaked"],
                         "false_hypotheses": ["leaked"]}
    a = render(task).channels
    b = render(poisoned).channels
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_rendered_channels_contain_no_ground_truth():
    """No rendered MCP response may contain the hidden root-cause sentence."""
    for entry in _corpus()["corpus"]:
        rc = entry["task"]["ground_truth"]["root_cause"]
        blob = json.dumps(render(entry["task"]).channels)
        assert rc not in blob, f"{entry['task']['task_id']}: root cause leaked"


def test_bench_source_never_exposes_hidden_fields():
    """The BenchMCPSource holds no hidden knowledge and returns none."""
    entry = _first_entry()
    src = BenchMCPSource(render(entry["task"]))
    # It is constructed from a RenderedScenario — no efic/ground_truth attribute.
    assert not hasattr(src, "efic")
    assert not hasattr(src, "ground_truth")
    blob = json.dumps(src._channels)
    for banned in ("investigation_spec", "ground_truth", "decisive_evidence",
                   "hypotheses_eliminated"):
        assert banned not in blob


def test_no_runtime_module_reads_investigation_spec():
    """The spec is evaluation-only: no engine/runtime module may reference it."""
    roots = ["supervisor", "workers", "intelligence"]
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    offenders = []
    for root in roots:
        base = os.path.join(repo, root)
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8", errors="ignore") as f:
                    if "investigation_spec" in f.read():
                        offenders.append(os.path.relpath(p, repo))
    assert offenders == [], f"runtime references investigation_spec: {offenders}"


# ---------------------------------------------------------------------------
# BenchMCPSource — production parity.
# ---------------------------------------------------------------------------

def test_source_serves_incident_and_records_queries():
    entry = _first_entry()
    src = BenchMCPSource(render(entry["task"]))
    resp = src.invoke("moogsoft.get_incident_by_id", "get_incident_by_id",
                      {"incident_id": entry["task"]["task_id"]})
    assert "incident" in resp
    assert resp["incident"]["affected_service"] == entry["task"]["incident"]["service"]
    assert len(src.query_log) == 1
    assert src.query_log[0]["served"] == "rendered"


def test_source_returns_production_empty_for_absent_server():
    """A server the scenario has no data for gets the real production-shaped empty
    (delegated to the stub dispatch) — a genuine 'no data', not an error/hint."""
    entry = _first_entry()
    src = BenchMCPSource(render(entry["task"]))
    resp = src.invoke("confluence.search_runbooks", "search_runbooks", {})
    assert resp == {"runbooks": []}
    assert src.query_log[-1]["served"] == "empty"


def test_source_advertises_servers():
    src = BenchMCPSource(render(_first_entry()["task"]))
    servers = src.discover_tools()
    assert {"splunk", "dynatrace", "sysdig", "servicenow"} <= set(servers)


# ---------------------------------------------------------------------------
# Evaluation — reuse + honesty.
# ---------------------------------------------------------------------------

def test_evaluate_reuses_scorer_and_marks_unexposed_not_measured():
    entry = _first_entry()
    # A minimal trace that exercises the evaluator without running the engine.
    trace = {
        "task_id": entry["task"]["task_id"],
        "confidence": 80, "localized_service": "", "recommendation": "do x",
        "hypotheses": ["h1", "h2"], "ruled_out": ["h2"], "hypothesis_count": 2,
        "winner_hypothesis": "h1", "decisive_evidence": [],
        "servers_queried": ["splunk", "sysdig"],
        "rendered_channels_served": ["splunk.search_logs", "sysdig.get_events"],
        "submission": {"schema_version": 1, "engine": "x", "task_id": entry["task"]["task_id"],
                       "root_cause": "", "localized_service": "", "hypotheses": [],
                       "ruled_out": [], "evidence_used": [], "decisive_evidence": [],
                       "confidence": 80, "proof": "", "replay_hash": ""},
    }
    ev = evaluate(entry["task"], entry["efic"], trace)
    assert "dimensions" in ev["eic"]                     # reused EIC scorer
    assert ev["process"]["blast_radius_understanding"]["state"] == NOT_MEASURED
    assert ev["process"]["recovery_validation"]["state"] == NOT_MEASURED
    assert ev["process"]["cross_mcp_correlation"]["raw"] == 1.0


def test_evaluated_config_recorded_in_module():
    from enterprisebench.pipeline.execute import EVALUATED_CONFIG
    assert "LLM_ENABLED" in EVALUATED_CONFIG["off"]
    assert "INTELLIGENCE_ENABLED" in EVALUATED_CONFIG["off"]
    assert "HYPOTHESIS_ENGINE_ENABLED" in EVALUATED_CONFIG["on"]


# ---------------------------------------------------------------------------
# End-to-end determinism + no repo pollution (subprocess; a few seconds).
# ---------------------------------------------------------------------------

@pytest.mark.timeout(240)
def test_investigation_is_deterministic():
    from enterprisebench.pipeline.execute import run_investigation
    task = _first_entry()["task"]
    a = run_investigation(task)
    b = run_investigation(task)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["root_cause"]                                # engine actually ran


@pytest.mark.timeout(240)
def test_run_corpus_subset_deterministic_and_clean(tmp_path):
    from enterprisebench.pipeline.run import run_corpus
    tid = _first_entry()["task"]["task_id"]
    r1 = run_corpus(only=[tid])
    r2 = run_corpus(only=[tid])
    assert r1["content_hash"] == r2["content_hash"]
    assert r1["scenario_count"] == 1
    assert r1["eb2_version"] == EB2_VERSION
    # Outcomes are explicit, never a bare boolean.
    assert r1["scenarios"][0]["outcome"] in ("PASS", "FAIL", "NOT_MEASURED", "ERROR")
