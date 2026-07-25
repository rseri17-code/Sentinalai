"""EnterpriseBench — EB-0 baseline evaluation runner.

EB-0 is the smallest executable slice of EnterpriseBench (see
`docs/enterprisebench/ARCHITECTURE.md`). It COMPOSES existing SentinelAI assets —
the enterprise corpus (`eval/enterprise/`) and the EIC scorer
(`sentinel_core.eic`) — into a deterministic, offline, CI-friendly runner that
produces a machine-readable baseline of investigation quality. It treats
SentinelAI as the system under test and modifies nothing in the engine, replay,
evidence, confidence, or planner.

Honest boundary: the enterprise corpus telemetry is NOT yet wired into the live
investigation engine (that injection is EB-2, `BenchMCPSource`, not built). EB-0
therefore evaluates *supplied* neutral submissions and reports `NOT_MEASURED`
per scenario when no engine submission is available — it never fabricates an
investigation. EnterpriseBench measures deterministic investigation correctness
and regression behavior; it does NOT prove real operator trust, adoption,
real-world MTTI reduction, or production effectiveness — those remain
`NOT_MEASURED` until pilot evidence exists.
"""
from enterprisebench.loader import CorpusError, load_corpus, validate_corpus
from enterprisebench.runner import (
    ERROR,
    FAIL,
    NOT_MEASURED,
    PASS,
    SKIPPED,
    UNSUPPORTED,
    file_provider,
    no_engine_provider,
    run,
)

EB_VERSION = "0.1.0"  # EnterpriseBench version (EB-0)

# CI exit codes (documented in docs/enterprisebench/ARCHITECTURE.md §EB-0)
EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_INVALID_CORPUS = 2
EXIT_ERROR = 3

__all__ = [
    "EB_VERSION", "run", "load_corpus", "validate_corpus", "CorpusError",
    "no_engine_provider", "file_provider",
    "PASS", "FAIL", "SKIPPED", "UNSUPPORTED", "NOT_MEASURED", "ERROR",
    "EXIT_OK", "EXIT_REGRESSION", "EXIT_INVALID_CORPUS", "EXIT_ERROR",
]
