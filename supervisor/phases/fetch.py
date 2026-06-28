"""Phase: FETCH — incident lookup and initial context.

SCAFFOLD ONLY. No behavior moves in Phase 7.

Today the equivalent of this phase lives at
``SentinalAISupervisor._fetch_incident`` (supervisor/agent.py:1893–1919).
It calls the ops_worker via ``self._call_worker()``, mutates ``receipts``
and ``budget``, and uses circuit breakers — all things that this module
cannot host while extraction would change runtime behavior.

When this phase is eventually moved out, the handler signature will be:

    def run(inp: PhaseInput) -> PhaseResult: ...

with ``inp.ctx.incident_id`` as the lookup key.
"""
from __future__ import annotations

__all__: list[str] = []
