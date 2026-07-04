"""SentinelBench scenario schema.

A **Scenario** is a self-contained JSON description of one synthetic
incident used to evaluate SentinelAI's RCA quality, evidence
completeness, red-herring resistance, confidence calibration, decision
trace quality, runtime cost, and MTTI.

Every scenario file lives under ``tests/synthetic/scenarios/*.json``.
The runner validates each file against :class:`Scenario` before scoring.

Schema fields
-------------
- ``scenario_id`` (str): matches the filename stem.
- ``title`` (str): one-line human-readable description.
- ``incident_input`` (dict): what would normally arrive at ``investigate()``
  (incident_id, service, incident_type, summary, ...).
- ``mocked_evidence_sources`` (dict): the offline stand-in for
  Splunk/Prometheus/k8s data — used only for documentation; scoring
  compares reported evidence keys against ``required_evidence``.
- ``expected_root_cause`` (str): ground-truth RCA sentence.
- ``required_evidence`` (list[str]): evidence keys that MUST appear.
- ``red_herrings`` (list[str]): evidence keys / RCA fragments that should
  NOT drive the reported root cause.
- ``expected_confidence_range`` (tuple[int, int]): inclusive [min, max]
  where a well-calibrated confidence should land (0-100).
- ``expected_decision_signals`` (list[str]): decision-context signal
  names the investigation is expected to raise.
- ``expected_mtti_budget_ms`` (int): upper bound on mean-time-to-identify.
- ``expected_runtime_cost_budget`` (int): upper bound on the runtime
  cost score (a unitless proxy the harness treats as "steps" or
  "tool calls").
- ``tags`` (list[str]): free-form labels for filtering.
- ``mock_investigation_output`` (dict, optional): a pre-baked "ideal
  investigation output" the harness uses when no external investigation
  output is provided. This is what makes CI runs deterministic without
  invoking the real ``investigate()``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SCENARIO_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class ScenarioSchemaError(ValueError):
    """Raised when a scenario JSON fails schema validation."""


# ---------------------------------------------------------------------------
# Scenario dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """A single synthetic scenario. All fields required; defaults keep
    the dataclass constructible for tests."""
    scenario_id:                   str
    title:                         str
    incident_input:                dict[str, Any] = field(default_factory=dict)
    mocked_evidence_sources:       dict[str, Any] = field(default_factory=dict)
    expected_root_cause:           str = ""
    required_evidence:             tuple[str, ...] = ()
    red_herrings:                  tuple[str, ...] = ()
    expected_confidence_range:     tuple[int, int] = (0, 100)
    expected_decision_signals:     tuple[str, ...] = ()
    expected_mtti_budget_ms:       int = 60_000
    expected_runtime_cost_budget:  int = 100
    tags:                          tuple[str, ...] = ()
    mock_investigation_output:     dict[str, Any] = field(default_factory=dict)
    schema_version:                int = SCENARIO_SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, tuple):
                d[k] = list(v)
        return d

    # ------------------------------------------------------------------
    # Factory + validation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        """Build a :class:`Scenario` from a raw dict and validate it."""
        validate_scenario_dict(data)
        return cls(
            scenario_id=str(data["scenario_id"]),
            title=str(data["title"]),
            incident_input=dict(data.get("incident_input", {})),
            mocked_evidence_sources=dict(data.get("mocked_evidence_sources", {})),
            expected_root_cause=str(data.get("expected_root_cause", "")),
            required_evidence=tuple(str(x) for x in data.get("required_evidence", [])),
            red_herrings=tuple(str(x) for x in data.get("red_herrings", [])),
            expected_confidence_range=_normalise_range(
                data.get("expected_confidence_range", [0, 100])
            ),
            expected_decision_signals=tuple(
                str(x) for x in data.get("expected_decision_signals", [])
            ),
            expected_mtti_budget_ms=int(data.get("expected_mtti_budget_ms", 60_000)),
            expected_runtime_cost_budget=int(
                data.get("expected_runtime_cost_budget", 100)
            ),
            tags=tuple(str(x) for x in data.get("tags", [])),
            mock_investigation_output=dict(
                data.get("mock_investigation_output", {})
            ),
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "Scenario":
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScenarioSchemaError(
                f"scenario {p.name}: invalid JSON: {exc}"
            ) from exc
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REQUIRED_KEYS: tuple[str, ...] = (
    "scenario_id",
    "title",
    "incident_input",
    "mocked_evidence_sources",
    "expected_root_cause",
    "required_evidence",
    "red_herrings",
    "expected_confidence_range",
    "expected_decision_signals",
    "expected_mtti_budget_ms",
    "expected_runtime_cost_budget",
    "tags",
)


def validate_scenario_dict(data: dict[str, Any]) -> None:
    """Validate a raw scenario dict. Raises ScenarioSchemaError."""
    if not isinstance(data, dict):
        raise ScenarioSchemaError("scenario must be a dict at top level")

    missing = [k for k in _REQUIRED_KEYS if k not in data]
    if missing:
        raise ScenarioSchemaError(
            f"scenario missing required keys: {sorted(missing)}"
        )

    # Types
    if not isinstance(data["scenario_id"], str) or not data["scenario_id"]:
        raise ScenarioSchemaError("scenario_id must be a non-empty string")
    if not isinstance(data["title"], str) or not data["title"]:
        raise ScenarioSchemaError("title must be a non-empty string")
    if not isinstance(data["incident_input"], dict):
        raise ScenarioSchemaError("incident_input must be a dict")
    if not isinstance(data["mocked_evidence_sources"], dict):
        raise ScenarioSchemaError("mocked_evidence_sources must be a dict")
    if not isinstance(data["expected_root_cause"], str):
        raise ScenarioSchemaError("expected_root_cause must be a string")
    if not isinstance(data["required_evidence"], list):
        raise ScenarioSchemaError("required_evidence must be a list")
    if not isinstance(data["red_herrings"], list):
        raise ScenarioSchemaError("red_herrings must be a list")

    r = data["expected_confidence_range"]
    if not isinstance(r, (list, tuple)) or len(r) != 2:
        raise ScenarioSchemaError(
            "expected_confidence_range must be a length-2 list/tuple"
        )
    lo, hi = r
    if not (isinstance(lo, int) and isinstance(hi, int)):
        raise ScenarioSchemaError(
            "expected_confidence_range endpoints must be integers"
        )
    if not (0 <= lo <= hi <= 100):
        raise ScenarioSchemaError(
            "expected_confidence_range must satisfy 0 ≤ lo ≤ hi ≤ 100"
        )

    if not isinstance(data["expected_decision_signals"], list):
        raise ScenarioSchemaError("expected_decision_signals must be a list")
    if not isinstance(data["expected_mtti_budget_ms"], int) \
            or data["expected_mtti_budget_ms"] < 0:
        raise ScenarioSchemaError(
            "expected_mtti_budget_ms must be a non-negative integer"
        )
    if not isinstance(data["expected_runtime_cost_budget"], int) \
            or data["expected_runtime_cost_budget"] < 0:
        raise ScenarioSchemaError(
            "expected_runtime_cost_budget must be a non-negative integer"
        )
    if not isinstance(data["tags"], list):
        raise ScenarioSchemaError("tags must be a list")

    # Optional mock_investigation_output is a dict when present
    mio = data.get("mock_investigation_output")
    if mio is not None and not isinstance(mio, dict):
        raise ScenarioSchemaError(
            "mock_investigation_output must be a dict when present"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_range(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return (0, 100)
    lo, hi = value
    try:
        lo_i = int(lo)
        hi_i = int(hi)
    except (TypeError, ValueError):
        return (0, 100)
    if lo_i > hi_i:
        lo_i, hi_i = hi_i, lo_i
    lo_i = max(0, min(100, lo_i))
    hi_i = max(0, min(100, hi_i))
    return (lo_i, hi_i)


__all__ = [
    "SCENARIO_SCHEMA_VERSION",
    "Scenario",
    "ScenarioSchemaError",
    "validate_scenario_dict",
]
