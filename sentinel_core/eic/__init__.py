"""Enterprise Investigation Challenge (EIC) — the engine-agnostic benchmark
for enterprise incident investigation.

The benchmark core (``benchmark``) grades any engine from a neutral Task +
Submission format. The ``adapter`` is the only SentinelAI-coupled piece and
is imported explicitly, never by the core.
"""
from sentinel_core.eic.benchmark import (
    CATEGORIES,
    DIFFICULTY,
    EIC_SCHEMA_VERSION,
    leaderboard,
    make_submission,
    make_task,
    score_submission,
)

__all__ = [
    "EIC_SCHEMA_VERSION", "CATEGORIES", "DIFFICULTY",
    "make_task", "make_submission", "score_submission", "leaderboard",
]
