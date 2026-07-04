"""SentinelBench runner — scenario loader + orchestrator.

Offline. Deterministic. Never invokes external systems, network, LLM,
or any production connector. Never mutates production artifacts.

Public entry points:
- :func:`load_scenario`      — one JSON file → Scenario
- :func:`load_all_scenarios` — every JSON under ``scenarios/`` → dict
- :func:`run_scenario`       — score one scenario
- :func:`run_all_scenarios`  — score every scenario in the corpus
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from tests.synthetic.schemas import Scenario, ScenarioSchemaError
from tests.synthetic.scoring import ScoreCard, score_investigation


# The corpus lives alongside this file
SCENARIOS_DIR: Path = Path(__file__).parent / "scenarios"


# ---------------------------------------------------------------------------
# Scenario loading
# ---------------------------------------------------------------------------

def load_scenario(scenario_id: str, scenarios_dir: Path | str | None = None) -> Scenario:
    """Load one scenario by id. Raises ScenarioSchemaError on failure."""
    directory = Path(scenarios_dir) if scenarios_dir else SCENARIOS_DIR
    path = directory / f"{scenario_id}.json"
    if not path.exists():
        raise ScenarioSchemaError(f"scenario '{scenario_id}' not found at {path}")
    scenario = Scenario.from_json_file(path)
    if scenario.scenario_id != scenario_id:
        raise ScenarioSchemaError(
            f"scenario file '{path.name}' has scenario_id '{scenario.scenario_id}' "
            f"(expected '{scenario_id}')"
        )
    return scenario


def load_all_scenarios(scenarios_dir: Path | str | None = None) -> dict[str, Scenario]:
    """Load every scenario JSON under ``scenarios_dir``. Returns a dict
    keyed by scenario_id, sorted deterministically."""
    directory = Path(scenarios_dir) if scenarios_dir else SCENARIOS_DIR
    if not directory.exists():
        return {}
    scenarios: dict[str, Scenario] = {}
    for path in sorted(directory.glob("*.json")):
        sc = Scenario.from_json_file(path)
        scenarios[sc.scenario_id] = sc
    # Return a dict sorted by scenario_id (Python 3.7+ preserves insertion order)
    return {k: scenarios[k] for k in sorted(scenarios.keys())}


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------

def run_scenario(
    scenario: Scenario | str,
    investigation_output: Mapping[str, Any] | None = None,
    scenarios_dir: Path | str | None = None,
    weights: Mapping[str, float] | None = None,
) -> ScoreCard:
    """Score one scenario. ``scenario`` may be a :class:`Scenario` or a
    scenario_id.  If ``investigation_output`` is None the scenario's
    ``mock_investigation_output`` is used — this is the deterministic
    CI path."""
    if isinstance(scenario, str):
        scenario = load_scenario(scenario, scenarios_dir=scenarios_dir)
    return score_investigation(scenario, investigation_output, weights=weights)


def run_all_scenarios(
    investigation_outputs: Mapping[str, Mapping[str, Any]] | None = None,
    scenarios_dir: Path | str | None = None,
    weights: Mapping[str, float] | None = None,
) -> list[ScoreCard]:
    """Score every scenario in the corpus. ``investigation_outputs`` is
    an optional map scenario_id → investigation_output; missing entries
    fall back to the scenario's mock output. Returns ScoreCards sorted
    by scenario_id."""
    scenarios = load_all_scenarios(scenarios_dir=scenarios_dir)
    cards: list[ScoreCard] = []
    for sc_id in sorted(scenarios.keys()):
        io = None
        if investigation_outputs and sc_id in investigation_outputs:
            io = investigation_outputs[sc_id]
        cards.append(score_investigation(scenarios[sc_id], io, weights=weights))
    return cards


__all__ = [
    "SCENARIOS_DIR",
    "load_scenario",
    "load_all_scenarios",
    "run_scenario",
    "run_all_scenarios",
]
