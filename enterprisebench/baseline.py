"""EB-0 baseline comparison — explicit, deterministic, never auto-updating.

Compares a current report against a committed baseline_summary and reports
regressions using explicit thresholds. No statistical-significance logic in EB-0
(deferred to a later phase). Baselines are only created/replaced by an explicit
command — never automatically.
"""
from __future__ import annotations

from typing import Any, Mapping

from enterprisebench.report import baseline_summary
from enterprisebench.runner import FAIL, PASS

# Explicit, configurable, documented threshold: a composite drop larger than
# this counts as a score regression.
DEFAULT_SCORE_DROP = 0.02


def compare(current_report: Mapping[str, Any],
            baseline: Mapping[str, Any],
            *, score_drop: float = DEFAULT_SCORE_DROP) -> dict[str, Any]:
    """Return regressions of the current report vs a baseline_summary."""
    cur = baseline_summary(current_report)
    regressions: list[dict[str, Any]] = []

    # schema / corpus compatibility
    if cur.get("scorer_version") != baseline.get("scorer_version"):
        regressions.append({"kind": "scorer_version_change",
                            "from": baseline.get("scorer_version"),
                            "to": cur.get("scorer_version")})
    if cur.get("corpus_hash") != baseline.get("corpus_hash"):
        regressions.append({"kind": "corpus_changed",
                            "from": baseline.get("corpus_hash"),
                            "to": cur.get("corpus_hash")})

    # determinism / replay regressions
    if baseline.get("scorer_determinism_ok") and not cur.get("scorer_determinism_ok"):
        regressions.append({"kind": "determinism_regression"})
    if (baseline.get("replay_status") not in (None, cur.get("replay_status"))):
        regressions.append({"kind": "replay_status_change",
                            "from": baseline.get("replay_status"),
                            "to": cur.get("replay_status")})

    base_scn = baseline.get("scenarios", {})
    cur_scn = cur.get("scenarios", {})

    # missing scenarios (present in baseline, gone now)
    for sid in sorted(set(base_scn) - set(cur_scn)):
        regressions.append({"kind": "missing_scenario", "scenario_id": sid})

    for sid in sorted(base_scn):
        if sid not in cur_scn:
            continue
        b, c = base_scn[sid], cur_scn[sid]
        # pass -> fail
        if b.get("outcome") == PASS and c.get("outcome") == FAIL:
            regressions.append({"kind": "pass_to_fail", "scenario_id": sid})
        # newly unsupported / not-measured where it used to pass
        if b.get("outcome") == PASS and c.get("outcome") in ("UNSUPPORTED", "NOT_MEASURED", "ERROR"):
            regressions.append({"kind": "regressed_to_" + str(c.get("outcome")).lower(),
                                "scenario_id": sid})
        # score degradation beyond threshold
        bs, cs = b.get("composite"), c.get("composite")
        if isinstance(bs, (int, float)) and isinstance(cs, (int, float)) \
                and (bs - cs) > score_drop:
            regressions.append({"kind": "score_degradation", "scenario_id": sid,
                                "from": bs, "to": cs, "drop": round(bs - cs, 4)})

    return {"has_regression": bool(regressions), "regressions": regressions,
            "score_drop_threshold": score_drop}


__all__ = ["compare", "DEFAULT_SCORE_DROP"]
