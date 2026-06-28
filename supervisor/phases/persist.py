"""Phase: PERSIST — database / memory / knowledge graph writeback.

SCAFFOLD ONLY. No behavior moves in Phase 7.

The persist phase today is ``SentinalAISupervisor._persist_results``
(supervisor/agent.py:1281–1738) which writes to SQL, the knowledge graph,
episodic memory, and records experiences. Every writer depends on the
supervisor's worker handles, calibrator singleton, or knowledge-graph
module-level singleton — none extractable without behavior change.

Future handler signature:

    def run(inp: PhaseInput) -> PhaseResult:
        ...  # idempotent; persists inp.evidence + inp.extras["result"]
"""
from __future__ import annotations

__all__: list[str] = []
