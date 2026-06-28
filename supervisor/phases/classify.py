"""Phase: CLASSIFY — incident type / service determination.

SCAFFOLD ONLY. No behavior moves in Phase 7.

Today this lives at ``SentinalAISupervisor.investigate`` lines 439–494,
which calls ``classify_incident()`` from ``supervisor.tool_selector`` and
then primes experiences. The classification call itself is pure, but the
surrounding ITSM/Confluence context fetch and experience priming use
workers and TLS, which keeps this phase non-extractable for now.

When this phase moves out, the handler will set ``ctx.with_classified(
incident_type=..., service=..., severity=...)`` and return the updated
context in ``PhaseResult.output.result``.
"""
from __future__ import annotations

__all__: list[str] = []
