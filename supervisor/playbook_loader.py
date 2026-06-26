"""YAML playbook loader for SentinalAI.

Loads investigation playbooks from config/playbooks/*.yaml when
YAML_PLAYBOOKS_ENABLED=true (default: false).

Each YAML file must have the structure:
  name: <incident_type>
  steps:
    - worker: <worker_name>
      action: <action_name>
      label: <step_label>        # required
      query_hint: <optional>
      metric_hint: <optional>

The loaded structure is identical to INCIDENT_PLAYBOOKS in tool_selector.py,
so get_evolved_playbook() and strategy_evolver work without changes.

Rollback: set YAML_PLAYBOOKS_ENABLED=false to revert to hardcoded playbooks.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PLAYBOOKS_DIR = Path(os.environ.get(
    "PLAYBOOKS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "config", "playbooks"),
))

_REQUIRED_STEP_KEYS = {"worker", "action", "label"}
_ALLOWED_STEP_KEYS = _REQUIRED_STEP_KEYS | {"query_hint", "metric_hint"}


def load_yaml_playbooks(playbooks_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Load all *.yaml playbook files from playbooks_dir.

    Returns a dict identical in structure to INCIDENT_PLAYBOOKS.
    Raises ValueError with a descriptive message if any file is malformed.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required for YAML playbooks. Install with: pip install pyyaml"
        ) from exc

    target_dir = Path(playbooks_dir or _PLAYBOOKS_DIR)
    if not target_dir.is_dir():
        raise FileNotFoundError(f"Playbooks directory not found: {target_dir}")

    playbooks: dict[str, list[dict[str, Any]]] = {}
    yaml_files = sorted(target_dir.glob("*.yaml")) + sorted(target_dir.glob("*.yml"))

    if not yaml_files:
        raise FileNotFoundError(f"No .yaml playbook files found in {target_dir}")

    for path in yaml_files:
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as exc:
            raise ValueError(f"Failed to parse {path}: {exc}") from exc

        _validate_playbook(doc, path)
        name = str(doc["name"])
        # Strip optional keys not in INCIDENT_PLAYBOOKS schema
        steps = [
            {k: v for k, v in step.items() if k in _ALLOWED_STEP_KEYS}
            for step in doc["steps"]
        ]
        playbooks[name] = steps
        logger.debug("Loaded playbook: %s (%d steps)", name, len(steps))

    logger.info("YAML playbooks loaded: %d types from %s", len(playbooks), target_dir)
    return playbooks


def _validate_playbook(doc: Any, path: Path) -> None:
    """Raise ValueError if the playbook document is structurally invalid."""
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: root must be a mapping, got {type(doc).__name__}")
    if "name" not in doc:
        raise ValueError(f"{path}: missing required key 'name'")
    if "steps" not in doc or not isinstance(doc["steps"], list):
        raise ValueError(f"{path}: 'steps' must be a list")
    if not doc["steps"]:
        raise ValueError(f"{path}: playbook must have at least one step")

    for i, step in enumerate(doc["steps"]):
        if not isinstance(step, dict):
            raise ValueError(f"{path} step[{i}]: must be a mapping")
        missing = _REQUIRED_STEP_KEYS - set(step.keys())
        if missing:
            raise ValueError(f"{path} step[{i}]: missing required keys {missing}")
        if not step["worker"] or not step["action"] or not step["label"]:
            raise ValueError(f"{path} step[{i}]: worker/action/label must be non-empty strings")
