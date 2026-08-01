"""Domain Intelligence Engine — base abstractions.

A Domain Intelligence Module (DIM) encapsulates the complete investigative
expertise for ONE operational domain (Database, Kubernetes, Messaging, …). Modules
compose into the EXISTING investigation pipeline: they read a normalized, read-only
view of the already-collected evidence and contribute hypotheses that flow through
the engine's existing evidence-weighted scoring, elimination, and winner-selection
machinery (the confidence engine is reused, not replaced).

This replaces the analyzer-per-failure-mode growth pattern with reusable modules
so the engine can scale to many failure modes without an explosion of ad-hoc
analyzers. Each module is additive, flag-gated (default off ⇒ byte-identical), and
deterministic. Modules never hardcode benchmark cases — they key on universal
production signatures for their domain.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping


class EvidenceView:
    """A normalized, read-only view of collected evidence for domain modules.

    Modules should reason over ``text`` (universal signal matching) plus the typed
    accessors; they must not mutate the underlying evidence.
    """

    __slots__ = ("service", "logs", "metrics", "events", "changes", "_text")

    def __init__(self, service: str, logs: list, metrics: Mapping[str, Any],
                 events: list, changes: list) -> None:
        self.service = str(service or "unknown")
        self.logs = logs or []
        self.metrics = metrics or {}
        self.events = events or []
        self.changes = changes or []
        self._text: str | None = None

    @property
    def text(self) -> str:
        """Lowercased union of log messages, metric name=value pairs, and event
        messages — the substrate for universal-signature matching."""
        if self._text is None:
            parts: list[str] = [str(e.get("message", "")) for e in self.logs]
            for m in (self.metrics.get("metrics") or []):
                parts.append(f"{m.get('name', '')}={m.get('value', '')}")
            parts += [str(e.get("message", "")) for e in self.events]
            self._text = " ".join(parts).lower()
        return self._text

    def metric_value(self, name_substr: str) -> Any:
        """First metric whose name contains ``name_substr`` (else None)."""
        for m in (self.metrics.get("metrics") or []):
            if name_substr in str(m.get("name", "")).lower():
                return m.get("value")
        return None


class DomainModule:
    """Base class for a Domain Intelligence Module. Subclasses set ``name`` +
    ``flag_env`` and implement ``analyze``. All contribution flows through the
    engine's existing confidence/elimination machinery."""

    name: str = ""
    flag_env: str = ""

    def enabled(self) -> bool:
        return os.environ.get(self.flag_env, "false").lower() in ("1", "true", "yes")

    def analyze(self, view: EvidenceView) -> list[dict]:
        """Return domain hypotheses as plain dicts:
        ``{name, root_cause, base_score, evidence_refs, reasoning, recommendation}``.
        Fires ONLY on a real domain signal so clean evidence contributes nothing."""
        raise NotImplementedError


def _hyp(name: str, root_cause: str, base_score: int, evidence_refs: Iterable[str],
         reasoning: str, recommendation: str) -> dict:
    return {"name": name, "root_cause": root_cause, "base_score": base_score,
            "evidence_refs": list(evidence_refs), "reasoning": reasoning,
            "recommendation": recommendation}


__all__ = ["EvidenceView", "DomainModule", "_hyp"]
