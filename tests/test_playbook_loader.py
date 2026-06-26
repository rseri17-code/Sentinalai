"""Tests for supervisor/playbook_loader.py and YAML playbook integration.

Covers:
- Unit: load_yaml_playbooks() with valid and invalid YAML
- Unit: _validate_playbook() catches every structural error
- Integration: get_playbook() returns YAML-sourced steps when flag is on
- Regression: INCIDENT_PLAYBOOKS is still importable and unchanged
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from supervisor.playbook_loader import load_yaml_playbooks
from supervisor.tool_selector import INCIDENT_PLAYBOOKS, get_playbook


# =========================================================================
# Helpers
# =========================================================================

def _write_yaml(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content))
    return p


# =========================================================================
# Regression: hardcoded playbooks remain importable and structurally valid
# =========================================================================

class TestIncidentPlaybooksRegression:
    """INCIDENT_PLAYBOOKS must remain importable and structurally intact.

    test_tool_selector.py imports INCIDENT_PLAYBOOKS directly; we must not
    break that import or alter the structure of existing playbooks.
    """

    def test_importable(self):
        assert isinstance(INCIDENT_PLAYBOOKS, dict)

    def test_all_known_types_present(self):
        expected = {
            "timeout", "oomkill", "error_spike", "latency", "saturation",
            "network", "cascading", "missing_data", "flapping", "silent_failure",
        }
        assert expected.issubset(set(INCIDENT_PLAYBOOKS.keys()))

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_each_playbook_has_steps(self, incident_type):
        steps = INCIDENT_PLAYBOOKS[incident_type]
        assert isinstance(steps, list) and len(steps) > 0

    @pytest.mark.parametrize("incident_type", list(INCIDENT_PLAYBOOKS.keys()))
    def test_each_step_has_required_keys(self, incident_type):
        required = {"worker", "action", "label"}
        for i, step in enumerate(INCIDENT_PLAYBOOKS[incident_type]):
            missing = required - set(step.keys())
            assert not missing, (
                f"{incident_type} step[{i}] missing keys: {missing}"
            )


# =========================================================================
# Unit: load_yaml_playbooks — valid files
# =========================================================================

class TestLoadYamlPlaybooksValid:

    def test_single_minimal_playbook(self, tmp_path):
        _write_yaml(tmp_path, "custom.yaml", """
            name: custom
            steps:
              - worker: log_worker
                action: search_logs
                label: search_custom_logs
        """)
        result = load_yaml_playbooks(tmp_path)
        assert "custom" in result
        assert result["custom"][0]["worker"] == "log_worker"
        assert result["custom"][0]["action"] == "search_logs"
        assert result["custom"][0]["label"] == "search_custom_logs"

    def test_optional_hints_preserved(self, tmp_path):
        _write_yaml(tmp_path, "hints.yaml", """
            name: hints
            steps:
              - worker: metrics_worker
                action: query_metrics
                label: check_metrics
                metric_hint: cpu_usage
                query_hint: "cpu {service}"
        """)
        result = load_yaml_playbooks(tmp_path)
        step = result["hints"][0]
        assert step["metric_hint"] == "cpu_usage"
        assert step["query_hint"] == "cpu {service}"

    def test_unknown_keys_stripped(self, tmp_path):
        _write_yaml(tmp_path, "extra.yaml", """
            name: extra
            steps:
              - worker: log_worker
                action: search_logs
                label: search_logs
                unknown_key: should_be_stripped
        """)
        result = load_yaml_playbooks(tmp_path)
        assert "unknown_key" not in result["extra"][0]

    def test_description_field_ignored(self, tmp_path):
        _write_yaml(tmp_path, "withdesc.yaml", """
            name: withdesc
            description: "This is fine"
            steps:
              - worker: apm_worker
                action: get_golden_signals
                label: check_signals
        """)
        result = load_yaml_playbooks(tmp_path)
        assert "withdesc" in result

    def test_multiple_files_all_loaded(self, tmp_path):
        for name in ("alpha", "beta", "gamma"):
            _write_yaml(tmp_path, f"{name}.yaml", f"""
                name: {name}
                steps:
                  - worker: log_worker
                    action: search_logs
                    label: search_{name}
            """)
        result = load_yaml_playbooks(tmp_path)
        assert set(result.keys()) == {"alpha", "beta", "gamma"}

    def test_yml_extension_also_loaded(self, tmp_path):
        _write_yaml(tmp_path, "alt.yml", """
            name: alt
            steps:
              - worker: log_worker
                action: search_logs
                label: search_alt
        """)
        result = load_yaml_playbooks(tmp_path)
        assert "alt" in result

    def test_multiple_steps_preserved_in_order(self, tmp_path):
        _write_yaml(tmp_path, "ordered.yaml", """
            name: ordered
            steps:
              - worker: log_worker
                action: search_logs
                label: step_one
              - worker: apm_worker
                action: get_golden_signals
                label: step_two
              - worker: metrics_worker
                action: query_metrics
                label: step_three
        """)
        result = load_yaml_playbooks(tmp_path)
        labels = [s["label"] for s in result["ordered"]]
        assert labels == ["step_one", "step_two", "step_three"]

    def test_returns_correct_structure_type(self, tmp_path):
        _write_yaml(tmp_path, "typed.yaml", """
            name: typed
            steps:
              - worker: log_worker
                action: search_logs
                label: search
        """)
        result = load_yaml_playbooks(tmp_path)
        assert isinstance(result, dict)
        assert isinstance(result["typed"], list)
        assert isinstance(result["typed"][0], dict)


# =========================================================================
# Unit: load_yaml_playbooks — validation errors
# =========================================================================

class TestLoadYamlPlaybooksValidation:

    def test_missing_name_raises(self, tmp_path):
        _write_yaml(tmp_path, "noname.yaml", """
            steps:
              - worker: log_worker
                action: search_logs
                label: x
        """)
        with pytest.raises(ValueError, match="missing required key 'name'"):
            load_yaml_playbooks(tmp_path)

    def test_missing_steps_raises(self, tmp_path):
        _write_yaml(tmp_path, "nosteps.yaml", """
            name: nosteps
        """)
        with pytest.raises(ValueError, match="'steps' must be a list"):
            load_yaml_playbooks(tmp_path)

    def test_empty_steps_list_raises(self, tmp_path):
        _write_yaml(tmp_path, "emptysteps.yaml", """
            name: emptysteps
            steps: []
        """)
        with pytest.raises(ValueError, match="at least one step"):
            load_yaml_playbooks(tmp_path)

    def test_step_missing_worker_raises(self, tmp_path):
        _write_yaml(tmp_path, "noworker.yaml", """
            name: noworker
            steps:
              - action: search_logs
                label: x
        """)
        with pytest.raises(ValueError, match="missing required keys"):
            load_yaml_playbooks(tmp_path)

    def test_step_missing_action_raises(self, tmp_path):
        _write_yaml(tmp_path, "noaction.yaml", """
            name: noaction
            steps:
              - worker: log_worker
                label: x
        """)
        with pytest.raises(ValueError, match="missing required keys"):
            load_yaml_playbooks(tmp_path)

    def test_step_missing_label_raises(self, tmp_path):
        _write_yaml(tmp_path, "nolabel.yaml", """
            name: nolabel
            steps:
              - worker: log_worker
                action: search_logs
        """)
        with pytest.raises(ValueError, match="missing required keys"):
            load_yaml_playbooks(tmp_path)

    def test_step_empty_worker_raises(self, tmp_path):
        _write_yaml(tmp_path, "emptyworker.yaml", """
            name: emptyworker
            steps:
              - worker: ""
                action: search_logs
                label: x
        """)
        with pytest.raises(ValueError, match="worker/action/label must be non-empty"):
            load_yaml_playbooks(tmp_path)

    def test_root_not_mapping_raises(self, tmp_path):
        _write_yaml(tmp_path, "list_root.yaml", """
            - worker: log_worker
              action: search_logs
              label: x
        """)
        with pytest.raises(ValueError, match="root must be a mapping"):
            load_yaml_playbooks(tmp_path)

    def test_step_not_mapping_raises(self, tmp_path):
        _write_yaml(tmp_path, "stepstring.yaml", """
            name: stepstring
            steps:
              - "just a string"
        """)
        with pytest.raises(ValueError, match="must be a mapping"):
            load_yaml_playbooks(tmp_path)

    def test_directory_not_found_raises(self, tmp_path):
        missing = tmp_path / "does_not_exist"
        with pytest.raises(FileNotFoundError, match="not found"):
            load_yaml_playbooks(missing)

    def test_no_yaml_files_raises(self, tmp_path):
        (tmp_path / "readme.txt").write_text("hi")
        with pytest.raises(FileNotFoundError, match="No .yaml"):
            load_yaml_playbooks(tmp_path)

    def test_invalid_yaml_syntax_raises(self, tmp_path):
        (tmp_path / "broken.yaml").write_text("name: [\nsteps: {{{")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_yaml_playbooks(tmp_path)


# =========================================================================
# Unit: load real config/playbooks directory
# =========================================================================

class TestRealPlaybooksDir:
    """Smoke-test the checked-in YAML files in config/playbooks/."""

    _REPO_ROOT = Path(__file__).parent.parent
    _PLAYBOOKS_DIR = _REPO_ROOT / "config" / "playbooks"

    def test_real_dir_loads_without_error(self):
        result = load_yaml_playbooks(self._PLAYBOOKS_DIR)
        assert isinstance(result, dict)
        assert len(result) >= 10

    def test_all_ten_incident_types_present(self):
        result = load_yaml_playbooks(self._PLAYBOOKS_DIR)
        expected = {
            "timeout", "oomkill", "error_spike", "latency", "saturation",
            "network", "cascading", "missing_data", "flapping", "silent_failure",
        }
        assert expected.issubset(set(result.keys()))

    @pytest.mark.parametrize("incident_type", [
        "timeout", "oomkill", "error_spike", "latency", "saturation",
        "network", "cascading", "missing_data", "flapping", "silent_failure",
    ])
    def test_each_yaml_playbook_has_valid_steps(self, incident_type):
        result = load_yaml_playbooks(self._PLAYBOOKS_DIR)
        steps = result[incident_type]
        assert len(steps) >= 1
        for i, step in enumerate(steps):
            for key in ("worker", "action", "label"):
                assert key in step, f"{incident_type} step[{i}] missing '{key}'"
                assert step[key], f"{incident_type} step[{i}] '{key}' is empty"


# =========================================================================
# Integration: _get_active_playbooks / get_playbook with YAML flag
# =========================================================================

class TestGetPlaybookYamlIntegration:
    """With YAML_PLAYBOOKS_ENABLED=true, get_playbook() should use YAML steps."""

    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        """Clear the function-level cache between tests."""
        import supervisor.tool_selector as ts
        ts._get_active_playbooks.__dict__.clear()
        yield
        ts._get_active_playbooks.__dict__.clear()

    def test_yaml_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("YAML_PLAYBOOKS_ENABLED", raising=False)
        result = get_playbook("latency")
        # Should be exactly what's in INCIDENT_PLAYBOOKS
        assert result == INCIDENT_PLAYBOOKS["latency"]

    def test_yaml_enabled_returns_yaml_steps(self, monkeypatch):
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        # Pointing at the real config/playbooks dir — default path
        result = get_playbook("latency")
        assert isinstance(result, list)
        assert len(result) >= 1
        for step in result:
            assert "worker" in step
            assert "action" in step
            assert "label" in step

    def test_yaml_enabled_covers_all_hardcoded_types(self, monkeypatch):
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        for incident_type in INCIDENT_PLAYBOOKS:
            steps = get_playbook(incident_type)
            assert steps, f"get_playbook({incident_type!r}) returned empty with YAML enabled"

    def test_yaml_enabled_custom_dir_overrides(self, monkeypatch, tmp_path):
        _write_yaml(tmp_path, "latency.yaml", """
            name: latency
            steps:
              - worker: custom_worker
                action: custom_action
                label: custom_step
        """)
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        monkeypatch.setenv("PLAYBOOKS_DIR", str(tmp_path))
        # Reset the module-level path constant in playbook_loader
        import supervisor.playbook_loader as pl
        original_dir = pl._PLAYBOOKS_DIR
        pl._PLAYBOOKS_DIR = tmp_path
        try:
            result = get_playbook("latency")
            assert result[0]["worker"] == "custom_worker"
        finally:
            pl._PLAYBOOKS_DIR = original_dir

    def test_yaml_load_failure_falls_back_to_hardcoded(self, monkeypatch):
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        monkeypatch.setenv("PLAYBOOKS_DIR", "/nonexistent/path/to/nowhere")
        import supervisor.playbook_loader as pl
        original_dir = pl._PLAYBOOKS_DIR
        pl._PLAYBOOKS_DIR = Path("/nonexistent/path/to/nowhere")
        try:
            result = get_playbook("timeout")
            # Must fall back to hardcoded — not crash
            assert result == INCIDENT_PLAYBOOKS["timeout"]
        finally:
            pl._PLAYBOOKS_DIR = original_dir

    def test_yaml_unknown_type_returns_error_spike_fallback(self, monkeypatch):
        monkeypatch.setenv("YAML_PLAYBOOKS_ENABLED", "true")
        result = get_playbook("totally_unknown_type")
        # get_playbook falls back to error_spike for unknown types
        assert result is not None
        assert len(result) > 0
