"""WorkingMemory — structured investigation state shared across harness correction rounds.

This is NOT the final reflection record (that's HarnessReflection in agent_harness.py).
WorkingMemory tracks live investigation state — hypothesis, confirmed facts, open questions,
tools called, and confidence trajectory — so that each correction round can build on prior
knowledge rather than starting fresh.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkingMemory:
    """Structured investigation state shared across harness correction rounds."""
    incident_id: str
    current_hypothesis: str = ""
    confirmed_facts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    confidence_trajectory: list[float] = field(default_factory=list)
    round_num: int = 0

    def update_from_result(self, result: dict) -> None:
        """Extract and store key information from an RCA result dict."""
        # Update hypothesis from root_cause
        root_cause = result.get("root_cause", "")
        if root_cause:
            self.current_hypothesis = root_cause

        # Update confidence_trajectory from confidence (normalize 0-100 → 0.0-1.0)
        confidence = result.get("confidence")
        if confidence is not None:
            try:
                conf_float = float(confidence)
                # Treat values > 1.0 as percentage (0–100 scale)
                if conf_float > 1.0:
                    conf_float = conf_float / 100.0
                self.confidence_trajectory.append(round(conf_float, 4))
            except (TypeError, ValueError):
                pass

        # Extract open questions from _critique gaps if present
        critique = result.get("_critique", {})
        if isinstance(critique, dict):
            gaps = critique.get("gaps", [])
            if isinstance(gaps, list):
                for gap in gaps:
                    if gap and isinstance(gap, str) and gap not in self.open_questions:
                        self.open_questions.append(gap)

        # Extract confirmed facts from reasoning/evidence_timeline if present
        reasoning = result.get("reasoning", "")
        if reasoning and isinstance(reasoning, str):
            # Add a compact fact summarizing the reasoning if not already present
            fact = reasoning.strip()
            if fact and fact not in self.confirmed_facts:
                self.confirmed_facts.append(fact)

        evidence_timeline = result.get("evidence_timeline", [])
        if isinstance(evidence_timeline, list):
            for entry in evidence_timeline:
                if isinstance(entry, str) and entry and entry not in self.confirmed_facts:
                    self.confirmed_facts.append(entry)
                elif isinstance(entry, dict):
                    summary = entry.get("summary") or entry.get("description") or entry.get("event", "")
                    if summary and isinstance(summary, str) and summary not in self.confirmed_facts:
                        self.confirmed_facts.append(summary)

    def record_tool_called(self, tool_name: str) -> None:
        if tool_name not in self.tools_called:
            self.tools_called.append(tool_name)

    def add_confirmed_fact(self, fact: str) -> None:
        if fact and fact not in self.confirmed_facts:
            self.confirmed_facts.append(fact)

    def is_improving(self) -> bool:
        """Return True if confidence is trending upward across rounds."""
        if len(self.confidence_trajectory) < 2:
            return True
        return self.confidence_trajectory[-1] >= self.confidence_trajectory[-2]

    def to_context_dict(self) -> dict[str, Any]:
        """Return a dict suitable for injecting into evidence for reanalyze() calls."""
        return {
            "current_hypothesis": self.current_hypothesis,
            "confirmed_facts": self.confirmed_facts,
            "open_questions": self.open_questions,
            "tools_called": self.tools_called,
            "confidence_trajectory": self.confidence_trajectory,
            "round_num": self.round_num,
        }
