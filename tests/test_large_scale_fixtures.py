"""Tests validating the large-scale fixture files."""
import json
import pytest
from pathlib import Path
from supervisor.incident_model import Incident

FIXTURES = Path(__file__).parent / "fixtures"

VALID_SNOW_STATES   = {"1", "2", "3", "6", "7"}
VALID_PRB_STATES    = {"1", "2", "3", "4"}
VALID_MOOG_SEVERITIES = {
    "Critical", "Major", "High", "Warning", "Medium", "Minor", "Low", "Info"
}
SPLUNK_INDEX_NAMES = [
    "production", "kubernetes", "database", "network", "security",
    "infrastructure", "apm", "audit", "pipeline", "middleware",
]


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (pytest)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def snow_data():
    path = FIXTURES / "servicenow_incidents_1000.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def moog_data():
    path = FIXTURES / "moogsoft_incidents_1000.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def prb_data():
    path = FIXTURES / "problem_records_1000.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def splunk_data():
    path = FIXTURES / "splunk_logs_large.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def sysdig_data():
    path = FIXTURES / "sysdig_metrics_large.json"
    return json.loads(path.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow
# ─────────────────────────────────────────────────────────────────────────────

class TestServiceNowFixtures:
    def test_count(self, snow_data):
        assert len(snow_data) == 1000

    def test_infra_split(self, snow_data):
        infra = [r for r in snow_data if r["category"] == "Infrastructure"]
        assert len(infra) == 500

    def test_app_split(self, snow_data):
        app = [r for r in snow_data if r["category"] == "Application"]
        assert len(app) == 500

    def test_required_fields(self, snow_data):
        required = {"number", "short_description", "cmdb_ci", "priority", "state", "category"}
        for rec in snow_data:
            missing = required - rec.keys()
            assert not missing, f"Record {rec.get('number')} missing fields: {missing}"

    def test_parseable_by_incident_model(self, snow_data):
        for rec in snow_data:
            inc = Incident.from_servicenow(rec)
            assert inc.incident_id, f"Empty incident_id for {rec.get('number')}"

    def test_priority_range(self, snow_data):
        for rec in snow_data:
            assert rec["priority"] in (1, 2, 3, 4), (
                f"Priority {rec['priority']} out of range for {rec['number']}"
            )

    def test_state_values(self, snow_data):
        for rec in snow_data:
            assert rec["state"] in VALID_SNOW_STATES, (
                f"Invalid state '{rec['state']}' for {rec['number']}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Moogsoft
# ─────────────────────────────────────────────────────────────────────────────

class TestMoogsoftFixtures:
    def test_count(self, moog_data):
        assert len(moog_data) == 1000

    def test_infra_split(self, moog_data):
        infra = [r for r in moog_data if r["category"] == "Infrastructure"]
        assert len(infra) == 500

    def test_app_split(self, moog_data):
        app = [r for r in moog_data if r["category"] == "Application"]
        assert len(app) == 500

    def test_required_fields(self, moog_data):
        required = {"incident_id", "summary", "severity", "status", "affected_service", "start_time"}
        for rec in moog_data:
            missing = required - rec.keys()
            assert not missing, f"Record {rec.get('incident_id')} missing fields: {missing}"

    def test_parseable_by_incident_model(self, moog_data):
        for rec in moog_data:
            inc = Incident.from_moogsoft(rec)
            assert inc.incident_id, f"Empty incident_id for {rec.get('incident_id')}"

    def test_severity_values(self, moog_data):
        for rec in moog_data:
            assert rec["severity"] in VALID_MOOG_SEVERITIES, (
                f"Invalid severity '{rec['severity']}' for {rec['incident_id']}"
            )

    def test_correlated_alerts_range(self, moog_data):
        for rec in moog_data:
            assert 1 <= rec["correlated_alerts"] <= 120, (
                f"correlated_alerts {rec['correlated_alerts']} out of range for {rec['incident_id']}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Problem records
# ─────────────────────────────────────────────────────────────────────────────

class TestProblemRecordFixtures:
    def test_count(self, prb_data):
        assert len(prb_data) == 1000

    def test_required_fields(self, prb_data):
        required = {"number", "sys_id", "short_description", "description",
                    "state", "priority", "cmdb_ci", "assigned_to", "category"}
        for rec in prb_data:
            missing = required - rec.keys()
            assert not missing, f"Record {rec.get('number')} missing fields: {missing}"

    def test_related_incidents_format(self, prb_data):
        for rec in prb_data:
            related = rec["related_incidents"]
            assert isinstance(related, list), f"{rec['number']} related_incidents not a list"
            assert 1 <= len(related) <= 3, (
                f"{rec['number']} related_incidents length {len(related)} out of range"
            )
            for inc_num in related:
                assert isinstance(inc_num, str), f"{rec['number']} incident number not a string"
                assert inc_num.startswith("INC"), (
                    f"{rec['number']} related incident '{inc_num}' doesn't start with INC"
                )

    def test_state_values(self, prb_data):
        for rec in prb_data:
            assert rec["state"] in VALID_PRB_STATES, (
                f"Invalid state '{rec['state']}' for {rec['number']}"
            )

    def test_known_error_is_bool(self, prb_data):
        for rec in prb_data:
            assert isinstance(rec["known_error"], bool), (
                f"known_error is {type(rec['known_error'])} for {rec['number']}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Splunk logs
# ─────────────────────────────────────────────────────────────────────────────

class TestSplunkLogsFixtures:
    def test_indexes_present(self, splunk_data):
        assert "indexes" in splunk_data
        present = set(splunk_data["indexes"].keys())
        expected = set(SPLUNK_INDEX_NAMES)
        assert expected == present, f"Missing or extra indexes: {expected.symmetric_difference(present)}"

    def test_each_index_has_entries(self, splunk_data):
        for idx_name, entries in splunk_data["indexes"].items():
            assert len(entries) == 50, (
                f"Index '{idx_name}' has {len(entries)} entries, expected 50"
            )

    def test_log_entry_fields(self, splunk_data):
        required = {"_time", "host", "level", "message", "service", "index"}
        for idx_name, entries in splunk_data["indexes"].items():
            for entry in entries:
                missing = required - entry.keys()
                assert not missing, (
                    f"Entry in index '{idx_name}' missing fields: {missing}"
                )

    def test_incident_logs_present(self, splunk_data):
        assert "incident_logs" in splunk_data
        incident_logs = splunk_data["incident_logs"]
        assert len(incident_logs) == 50, (
            f"Expected 50 incident log groups, got {len(incident_logs)}"
        )
        for inc_id, log_group in incident_logs.items():
            assert "index" in log_group,            f"{inc_id}: missing 'index'"
            assert "results" in log_group,          f"{inc_id}: missing 'results'"
            assert "count" in log_group,            f"{inc_id}: missing 'count'"
            assert "first_occurrence" in log_group, f"{inc_id}: missing 'first_occurrence'"
            assert isinstance(log_group["results"], list), f"{inc_id}: results not a list"


# ─────────────────────────────────────────────────────────────────────────────
# Sysdig metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestSysdigMetricsFixtures:
    def test_services_present(self, sysdig_data):
        assert "services" in sysdig_data
        assert len(sysdig_data["services"]) == 30, (
            f"Expected 30 services, got {len(sysdig_data['services'])}"
        )

    def test_golden_signals_structure(self, sysdig_data):
        required_signals = {"latency", "traffic", "errors", "saturation"}
        for svc_name, svc in sysdig_data["services"].items():
            assert "golden_signals" in svc, f"Service '{svc_name}' missing golden_signals"
            gs = svc["golden_signals"]
            missing = required_signals - gs.keys()
            assert not missing, f"Service '{svc_name}' golden_signals missing: {missing}"
            # latency sub-fields
            lat = gs["latency"]
            for field in ("p50", "p95", "p99", "baseline_p95"):
                assert field in lat, f"Service '{svc_name}' latency missing '{field}'"

    def test_resource_metrics_are_list(self, sysdig_data):
        for svc_name, svc in sysdig_data["services"].items():
            assert "resource_metrics" in svc, f"Service '{svc_name}' missing resource_metrics"
            assert isinstance(svc["resource_metrics"], list), (
                f"Service '{svc_name}' resource_metrics is not a list"
            )
            assert len(svc["resource_metrics"]) > 0, (
                f"Service '{svc_name}' resource_metrics is empty"
            )

    def test_infrastructure_nodes(self, sysdig_data):
        assert "infrastructure" in sysdig_data
        infra = sysdig_data["infrastructure"]
        assert "nodes" in infra, "infrastructure missing 'nodes'"
        assert "cluster" in infra, "infrastructure missing 'cluster'"
        nodes = infra["nodes"]
        assert len(nodes) >= 5, f"Expected at least 5 nodes, got {len(nodes)}"
        for node_name, node in nodes.items():
            assert "resource_metrics" in node, f"Node '{node_name}' missing resource_metrics"
            assert isinstance(node["resource_metrics"], list)

    def test_incident_metrics_present(self, sysdig_data):
        assert "incident_metrics" in sysdig_data
        im = sysdig_data["incident_metrics"]
        assert len(im) == 50, f"Expected 50 incident metric groups, got {len(im)}"
        for inc_id, metrics in im.items():
            assert "golden_signals" in metrics, f"{inc_id}: missing golden_signals"
            assert "anomaly_detected" in metrics, f"{inc_id}: missing anomaly_detected"
