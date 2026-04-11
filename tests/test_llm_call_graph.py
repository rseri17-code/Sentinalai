"""Tests for supervisor.llm_call_graph."""
from __future__ import annotations

import time
import pytest

from supervisor.llm_call_graph import (
    CallGraph,
    LLMCallNode,
    current_graph,
    set_current_graph,
    _hash_prompt,
)


# ---------------------------------------------------------------------------
# LLMCallNode
# ---------------------------------------------------------------------------

class TestLLMCallNode:

    def test_total_tokens(self):
        node = LLMCallNode(
            call_id="abc", parent_id=None, purpose="rca",
            input_tokens=1000, output_tokens=400,
        )
        assert node.total_tokens == 1400

    def test_duration_ms_from_finished_at(self):
        node = LLMCallNode(call_id="abc", parent_id=None, purpose="rca")
        node.started_at = 0.0
        node.finished_at = 1.5
        assert node.duration_ms == pytest.approx(1500.0, abs=0.1)

    def test_duration_ms_falls_back_to_latency_ms(self):
        node = LLMCallNode(
            call_id="abc", parent_id=None, purpose="rca",
            latency_ms=2500.0, finished_at=0.0,
        )
        assert node.duration_ms == 2500.0

    def test_to_dict_includes_total_tokens(self):
        node = LLMCallNode(
            call_id="abc", parent_id=None, purpose="rca",
            input_tokens=100, output_tokens=50,
        )
        d = node.to_dict()
        assert d["total_tokens"] == 150
        assert "duration_ms" in d


# ---------------------------------------------------------------------------
# CallGraph
# ---------------------------------------------------------------------------

class TestCallGraph:

    def test_span_creates_node(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca_analysis") as call_id:
            assert call_id in graph._nodes

    def test_span_sets_parent_for_nested(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("outer") as outer_id:
            with graph.span("inner") as inner_id:
                inner_node = graph.get_node(inner_id)
                assert inner_node.parent_id == outer_id

    def test_root_nodes_parentless(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("root1"):
            pass
        with graph.span("root2"):
            pass
        roots = graph.root_nodes()
        assert len(roots) == 2
        for n in roots:
            assert n.parent_id is None

    def test_record_updates_tokens(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca") as call_id:
            graph.record(call_id, model="claude-sonnet-4-6",
                         input_tokens=1200, output_tokens=400, latency_ms=2100)
        node = graph.get_node(call_id)
        assert node.input_tokens == 1200
        assert node.output_tokens == 400
        assert node.model == "claude-sonnet-4-6"

    def test_total_tokens_sum(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("a") as id_a:
            graph.record(id_a, input_tokens=100, output_tokens=50)
        with graph.span("b") as id_b:
            graph.record(id_b, input_tokens=200, output_tokens=80)
        assert graph.total_tokens() == 430

    def test_duplicate_calls_detected(self):
        graph = CallGraph(investigation_id="INV001")
        prompt = "Analyse this incident: OOMKilled in payment-service"
        with graph.span("rca", prompt_prefix=prompt) as id1:
            pass
        with graph.span("rca", prompt_prefix=prompt) as id2:
            pass
        dupes = graph.duplicate_calls()
        assert len(dupes) >= 1

    def test_no_duplicate_calls_different_prompts(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca", prompt_prefix="prompt one") as _:
            pass
        with graph.span("rca", prompt_prefix="prompt two") as _:
            pass
        assert graph.duplicate_calls() == []

    def test_span_records_error_on_exception(self):
        graph = CallGraph(investigation_id="INV001")
        with pytest.raises(ValueError):
            with graph.span("failing") as call_id:
                raise ValueError("intentional test error")
        node = graph.get_node(call_id)
        assert "intentional test error" in node.error

    def test_span_sets_finished_at(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca") as call_id:
            pass
        node = graph.get_node(call_id)
        assert node.finished_at > 0

    def test_children_of(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("parent") as parent_id:
            with graph.span("child1") as child1_id:
                pass
            with graph.span("child2") as child2_id:
                pass
        children = graph.children_of(parent_id)
        child_ids = {c.call_id for c in children}
        assert child1_id in child_ids
        assert child2_id in child_ids

    def test_total_cost_estimate(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca") as cid:
            graph.record(cid, input_tokens=1_000_000, output_tokens=0)
        # 1M input tokens at $3/MTok = $3.00
        cost = graph.total_cost_estimate_usd(input_cost_per_mtok=3.0, output_cost_per_mtok=15.0)
        assert cost == pytest.approx(3.0, abs=0.001)

    def test_summary_by_purpose(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca_analysis") as id1:
            graph.record(id1, input_tokens=100, output_tokens=50)
        with graph.span("self_critique") as id2:
            graph.record(id2, input_tokens=80, output_tokens=30)
        summary = graph.summary()
        assert summary["total_calls"] == 2
        assert "rca_analysis" in summary["by_purpose"]
        assert "self_critique" in summary["by_purpose"]

    def test_summary_to_dict(self):
        import json
        graph = CallGraph(investigation_id="INV001")
        with graph.span("rca") as cid:
            graph.record(cid, input_tokens=10, output_tokens=5)
        # Should not raise
        json.dumps(graph.to_dict())

    def test_all_nodes(self):
        graph = CallGraph(investigation_id="INV001")
        with graph.span("a"):
            pass
        with graph.span("b"):
            pass
        assert len(graph.all_nodes()) == 2


# ---------------------------------------------------------------------------
# Thread-local current_graph
# ---------------------------------------------------------------------------

class TestCurrentGraph:

    def test_set_and_get(self):
        graph = CallGraph(investigation_id="TEST")
        set_current_graph(graph)
        assert current_graph() is graph

    def test_set_none_clears(self):
        graph = CallGraph(investigation_id="TEST")
        set_current_graph(graph)
        set_current_graph(None)
        assert current_graph() is None

    def test_default_is_none(self):
        set_current_graph(None)
        assert current_graph() is None


# ---------------------------------------------------------------------------
# _hash_prompt
# ---------------------------------------------------------------------------

class TestHashPrompt:

    def test_deterministic(self):
        assert _hash_prompt("test prompt") == _hash_prompt("test prompt")

    def test_different_prompts_different_hashes(self):
        assert _hash_prompt("prompt A") != _hash_prompt("prompt B")

    def test_truncates_at_200_chars(self):
        long = "x" * 500
        short = "x" * 200
        # Both should hash the same (truncated to 200)
        assert _hash_prompt(long) == _hash_prompt(short)

    def test_returns_16_char_hex(self):
        h = _hash_prompt("some text")
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)
