"""Tests for supervisor.working_memory.WorkingMemory."""
from __future__ import annotations

import pytest
from supervisor.working_memory import WorkingMemory


class TestInitialization:
    def test_initialization(self):
        wm = WorkingMemory(incident_id="inc-001")
        assert wm.incident_id == "inc-001"
        assert wm.current_hypothesis == ""
        assert wm.confirmed_facts == []
        assert wm.open_questions == []
        assert wm.tools_called == []
        assert wm.confidence_trajectory == []
        assert wm.round_num == 0


class TestUpdateFromResult:
    def test_update_from_result_extracts_hypothesis(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.update_from_result({"root_cause": "Database connection pool exhausted"})
        assert wm.current_hypothesis == "Database connection pool exhausted"

    def test_update_from_result_tracks_confidence(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.update_from_result({"confidence": 75})
        assert wm.confidence_trajectory == [0.75]

    def test_update_from_result_tracks_confidence_decimal(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.update_from_result({"confidence": 0.6})
        assert wm.confidence_trajectory == [0.6]

    def test_update_from_result_extracts_gaps(self):
        wm = WorkingMemory(incident_id="inc-001")
        result = {
            "_critique": {
                "gaps": ["Missing log evidence", "No metric data for window"]
            }
        }
        wm.update_from_result(result)
        assert "Missing log evidence" in wm.open_questions
        assert "No metric data for window" in wm.open_questions

    def test_update_from_result_no_duplicate_gaps(self):
        wm = WorkingMemory(incident_id="inc-001")
        result = {"_critique": {"gaps": ["Same gap", "Same gap"]}}
        wm.update_from_result(result)
        assert wm.open_questions.count("Same gap") == 1

    def test_update_from_result_accumulates_confidence_across_calls(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.update_from_result({"confidence": 60})
        wm.update_from_result({"confidence": 80})
        assert wm.confidence_trajectory == [0.6, 0.8]

    def test_update_from_result_empty_dict(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.update_from_result({})
        assert wm.current_hypothesis == ""
        assert wm.confidence_trajectory == []
        assert wm.open_questions == []

    def test_update_from_result_extracts_evidence_timeline_strings(self):
        wm = WorkingMemory(incident_id="inc-001")
        result = {"evidence_timeline": ["CPU spiked at 14:02", "Pod OOMKilled at 14:03"]}
        wm.update_from_result(result)
        assert "CPU spiked at 14:02" in wm.confirmed_facts
        assert "Pod OOMKilled at 14:03" in wm.confirmed_facts

    def test_update_from_result_extracts_evidence_timeline_dicts(self):
        wm = WorkingMemory(incident_id="inc-001")
        result = {"evidence_timeline": [{"summary": "High error rate detected"}]}
        wm.update_from_result(result)
        assert "High error rate detected" in wm.confirmed_facts


class TestRecordToolCalled:
    def test_record_tool_called_deduplicates(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.record_tool_called("log_worker")
        wm.record_tool_called("log_worker")
        assert wm.tools_called == ["log_worker"]

    def test_record_tool_called_multiple_different(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.record_tool_called("log_worker")
        wm.record_tool_called("metrics_worker")
        assert wm.tools_called == ["log_worker", "metrics_worker"]


class TestIsImproving:
    def test_is_improving_true_when_rising(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.confidence_trajectory = [0.5, 0.7]
        assert wm.is_improving() is True

    def test_is_improving_false_when_falling(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.confidence_trajectory = [0.7, 0.5]
        assert wm.is_improving() is False

    def test_is_improving_true_when_equal(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.confidence_trajectory = [0.6, 0.6]
        assert wm.is_improving() is True

    def test_is_improving_true_with_single_entry(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.confidence_trajectory = [0.5]
        assert wm.is_improving() is True

    def test_is_improving_true_with_no_entries(self):
        wm = WorkingMemory(incident_id="inc-001")
        assert wm.is_improving() is True


class TestToContextDict:
    def test_to_context_dict_structure(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.current_hypothesis = "Hypothesis A"
        wm.confirmed_facts = ["fact1"]
        wm.open_questions = ["q1"]
        wm.tools_called = ["tool_x"]
        wm.confidence_trajectory = [0.6, 0.75]
        wm.round_num = 2

        ctx = wm.to_context_dict()

        assert set(ctx.keys()) == {
            "current_hypothesis",
            "confirmed_facts",
            "open_questions",
            "tools_called",
            "confidence_trajectory",
            "round_num",
        }
        assert ctx["current_hypothesis"] == "Hypothesis A"
        assert ctx["confirmed_facts"] == ["fact1"]
        assert ctx["open_questions"] == ["q1"]
        assert ctx["tools_called"] == ["tool_x"]
        assert ctx["confidence_trajectory"] == [0.6, 0.75]
        assert ctx["round_num"] == 2

    def test_to_context_dict_does_not_include_incident_id(self):
        wm = WorkingMemory(incident_id="inc-secret")
        ctx = wm.to_context_dict()
        assert "incident_id" not in ctx


class TestAddConfirmedFact:
    def test_add_confirmed_fact_deduplicates(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.add_confirmed_fact("Service X crashed")
        wm.add_confirmed_fact("Service X crashed")
        assert wm.confirmed_facts == ["Service X crashed"]

    def test_add_confirmed_fact_ignores_empty_string(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.add_confirmed_fact("")
        assert wm.confirmed_facts == []

    def test_add_confirmed_fact_multiple_unique(self):
        wm = WorkingMemory(incident_id="inc-001")
        wm.add_confirmed_fact("Fact A")
        wm.add_confirmed_fact("Fact B")
        assert wm.confirmed_facts == ["Fact A", "Fact B"]
