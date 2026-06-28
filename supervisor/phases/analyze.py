"""Phase: ANALYZE — hypothesis generation, scoring, RCA.

SCAFFOLD ONLY. No behavior moves in Phase 7.

The analyze phase today is ``SentinalAISupervisor._analyze_evidence``
(supervisor/agent.py:2683–2970) plus hypothesis refinement, confidence
calibration, citation annotation, and self-critique. It mutates TLS
state, calls the LLM, mutates the evidence dict, and chains into the
analyzer methods (``_analyze_timeout``, ``_analyze_oomkill``, ...). None
of that is movable without rewiring shared state.

The pure ``compute_confidence`` scoring function has been extracted to
``supervisor.helpers.confidence`` and is re-exported from ``supervisor.agent``.

Future handler signature:

    def run(inp: PhaseInput) -> PhaseResult:
        ...  # returns PhaseOutput(result={root_cause, confidence, ...})
"""
from __future__ import annotations

__all__: list[str] = []
