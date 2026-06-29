"""Shadow-mode helpers for the typed evidence ledger.

Phase 8 ships the feature-flag plumbing only. The actual shadow path that
would build an EvidenceLedger alongside the legacy evidence dict in
``supervisor.agent`` is **not wired** in this phase — wiring it requires
touching ``investigate()`` construction sites, which is forbidden by the
Phase 8 stop conditions.

When a future phase enables this:

    if is_shadow_enabled():
        shadow = dict_to_ledger(evidence)
        # ... compare ledger.to_dict() vs evidence at end of phase ...
        # ... log mismatches (must be empty) ...

The intent is parity validation only — the ledger MUST NOT influence
decisions, output, or persistence in shadow mode.
"""
from __future__ import annotations

import os


SHADOW_ENV_VAR = "EVIDENCE_LEDGER_SHADOW_ENABLED"


def is_shadow_enabled() -> bool:
    """Return True if the shadow ledger should be constructed.

    Off by default. Set ``EVIDENCE_LEDGER_SHADOW_ENABLED=true`` (or ``1``,
    ``yes``, ``on``) to enable.
    """
    val = os.environ.get(SHADOW_ENV_VAR, "").strip().lower()
    return val in ("1", "true", "yes", "on")


__all__ = ["is_shadow_enabled", "SHADOW_ENV_VAR"]
