"""Phase: COLLECT — playbook / planner evidence gathering.

SCAFFOLD ONLY. No behavior moves in Phase 7.

The collect phase today is investigate() lines 502–703 plus the playbook
executor (``_execute_playbook`` 2411–2551) and the planner loop
(``_execute_planner_loop`` 2361–2410). Both run workers in parallel via
``self._executor``, mutate ``receipts`` / ``budget`` / ``circuits``, and
accumulate into the evidence dict by direct mutation. Moving any of this
out would change runtime behavior or worker ordering, which is forbidden
this phase.

Future handler signature:

    def run(inp: PhaseInput) -> PhaseResult:
        ...  # returns PhaseOutput(evidence=collected_evidence)
"""
from __future__ import annotations

__all__: list[str] = []
