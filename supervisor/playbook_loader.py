"""YAML playbook loader for SentinalAI.

Loads investigation playbooks from config/playbooks/*.yaml when
YAML_PLAYBOOKS_ENABLED=true (default: false).

Each YAML file must have the structure:
  name: <incident_type>
  steps:
    - worker: <worker_name_or_alias>
      action: <action_name>
      label: <step_label>        # required
      query_hint: <optional>
      metric_hint: <optional>

Worker names may be canonical supervisor keys (log_worker, apm_worker, …)
or vendor-neutral aliases (splunk, logs, prometheus, …). Aliases resolve
only on this load path — hardcoded INCIDENT_PLAYBOOKS are never rewritten.

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

_DEFAULT_ALIASES_PATH = Path(
    os.path.join(os.path.dirname(__file__), "..", "config", "worker_aliases.yaml"),
)

_REQUIRED_STEP_KEYS = {"worker", "action", "label"}
_ALLOWED_STEP_KEYS = _REQUIRED_STEP_KEYS | {"query_hint", "metric_hint"}

# Worker names registered on SentinalAISupervisor (and planner aliases).
CANONICAL_WORKERS: frozenset[str] = frozenset({
    "ops_worker",
    "log_worker",
    "metrics_worker",
    "apm_worker",
    "knowledge_worker",
    "itsm_worker",
    "devops_worker",
    "confluence_worker",
    "code_worker",
    "git_worker",
    "network_worker",
    "signal_worker",
    "event_worker",
    "change_worker",
})

# Vendor-neutral / vendor-named step.worker values → canonical supervisor keys.
# Applied only when loading YAML playbooks. Overlay with config/worker_aliases.yaml
# or WORKER_ALIASES_PATH — do not fork playbooks to rename a log backend.
DEFAULT_WORKER_ALIASES: dict[str, str] = {
    "logs": "log_worker",
    "log": "log_worker",
    "splunk": "log_worker",
    "elasticsearch": "log_worker",
    "elk": "log_worker",
    "loki": "log_worker",
    "metrics": "metrics_worker",
    "prometheus": "metrics_worker",
    "sysdig": "metrics_worker",
    "datadog": "metrics_worker",
    "cloudwatch": "metrics_worker",
    "apm": "apm_worker",
    "dynatrace": "apm_worker",
    "signalfx": "apm_worker",
    "newrelic": "apm_worker",
    "traces": "apm_worker",
    "itsm": "itsm_worker",
    "servicenow": "itsm_worker",
    "jira": "itsm_worker",
    "pagerduty": "itsm_worker",
    "ops": "ops_worker",
    "moogsoft": "ops_worker",
    "events": "event_worker",
    "devops": "devops_worker",
    "github": "devops_worker",
    "gitlab": "devops_worker",
    "ci": "devops_worker",
    "git": "git_worker",
    "confluence": "confluence_worker",
    "wiki": "confluence_worker",
    "docs": "confluence_worker",
    "knowledge": "knowledge_worker",
    "network": "network_worker",
    "thousandeyes": "network_worker",
    "code": "code_worker",
    "signals": "signal_worker",
    "changes": "change_worker",
}


def _aliases_path() -> Path:
    override = os.environ.get("WORKER_ALIASES_PATH", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_ALIASES_PATH


def load_worker_aliases(path: Path | None = None) -> dict[str, str]:
    """Return alias → canonical worker map (defaults plus optional YAML overlay)."""
    aliases = dict(DEFAULT_WORKER_ALIASES)
    target = Path(path) if path is not None else _aliases_path()
    if not target.is_file():
        return aliases

    try:
        import yaml
        with open(target, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception as exc:
        logger.warning("Worker alias file %s unreadable (%s); using defaults", target, exc)
        return aliases

    extra = doc
    if isinstance(doc, dict) and isinstance(doc.get("aliases"), dict):
        extra = doc["aliases"]
    if not isinstance(extra, dict):
        logger.warning("Worker alias file %s: expected a mapping; using defaults", target)
        return aliases

    for key, value in extra.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key, value = key.strip(), value.strip()
        if key and value:
            aliases[key] = value
            aliases[key.lower()] = value
    return aliases


def resolve_worker_alias(name: str, aliases: dict[str, str] | None = None) -> str:
    """Map a YAML step.worker value to a supervisor worker key.

    Canonical names pass through. Unknown names are left unchanged so a
    custom worker in a test or overlay playbook still loads.
    """
    key = str(name).strip()
    if not key:
        return key
    if key in CANONICAL_WORKERS:
        return key
    mapping = aliases if aliases is not None else load_worker_aliases()
    return mapping.get(key, mapping.get(key.lower(), key))


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

    aliases = load_worker_aliases()

    for path in yaml_files:
        try:
            with open(path, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except Exception as exc:
            raise ValueError(f"Failed to parse {path}: {exc}") from exc

        _validate_playbook(doc, path)
        name = str(doc["name"])
        # Strip optional keys not in INCIDENT_PLAYBOOKS schema; resolve aliases.
        steps = []
        for step in doc["steps"]:
            cleaned = {k: v for k, v in step.items() if k in _ALLOWED_STEP_KEYS}
            cleaned["worker"] = resolve_worker_alias(str(cleaned["worker"]), aliases)
            steps.append(cleaned)
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
