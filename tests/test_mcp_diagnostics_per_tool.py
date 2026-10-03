"""Per-tool connection diagnostics (contract v1.2).

Worker rollup stays the worst required tool. Optional tools are reported
and do not move that rollup. ``WORKER_TOOLS`` entries stay required.
"""

from __future__ import annotations

from workers.mcp_diagnostics import (
    diagnose,
    format_text,
    replay_hash,
    report_exit_code,
    worst_state,
)


def _listed(*names: str) -> list[str]:
    return list(names)


class TestWorstState:
    def test_rank_order(self) -> None:
        assert worst_state([]) == "reachable"
        assert worst_state(["reachable", "stubbed"]) == "stubbed"
        assert worst_state(["stubbed", "missing"]) == "missing"
        assert worst_state(["missing", "failed", "reachable"]) == "failed"

    def test_optional_failure_does_not_move_the_worker(self) -> None:
        report = diagnose(
            workers={"metrics_worker": frozenset({"sysdig"})},
            tools_for={"metrics_worker": ("sysdig.query_metrics", "sysdig.get_events")},
            optional_tools=frozenset({"sysdig.get_events"}),
            list_tools=lambda: _listed(
                "SysdigTarget___query_metrics",
                "SysdigTarget___get_events",
            ),
            call_tool=lambda name: (
                {"error": "probe blew up", "error_class": "RuntimeError"}
                if name.endswith("get_events")
                else {"metrics": {"request_rate": 1}}
            ),
            configured=True,
            assume_endpoint=True,
        )
        worker = report["workers"][0]
        by_tool = {row["tool"]: row for row in report["tools"]}
        assert worker["state"] == "reachable"
        assert by_tool["sysdig.query_metrics"]["state"] == "reachable"
        assert by_tool["sysdig.query_metrics"]["required"] is True
        assert by_tool["sysdig.get_events"]["state"] == "failed"
        assert by_tool["sysdig.get_events"]["required"] is False
        assert report_exit_code(report) == 0

    def test_required_failure_beats_a_missing_required_tool(self) -> None:
        report = diagnose(
            workers={"log_worker": frozenset({"splunk"})},
            tools_for={"log_worker": ("splunk.search_oneshot", "splunk.get_change_data")},
            list_tools=lambda: _listed(
                "SplunkTarget___search_oneshot",
                "SplunkTarget___get_change_data",
            ),
            call_tool=lambda name: (
                {"error": "loki down", "error_class": "ConnectionError"}
                if name.endswith("search_oneshot")
                else {"skipped": True, "changes": []}
            ),
            configured=True,
            assume_endpoint=True,
        )
        assert report["workers"][0]["state"] == "failed"
        assert report["workers"][0]["error_class"] == "ConnectionError"
        assert report_exit_code(report) == 2


class TestToolRows:
    def test_rows_sort_by_worker_then_tool(self) -> None:
        report = diagnose(
            workers={
                "log_worker": frozenset({"splunk"}),
                "ops_worker": frozenset({"moogsoft"}),
            },
            tools_for={
                "log_worker": ("splunk.get_change_data", "splunk.search_oneshot"),
                "ops_worker": ("moogsoft.get_incident_by_id",),
            },
            list_tools=lambda: _listed(
                "SplunkTarget___get_change_data",
                "SplunkTarget___search_oneshot",
                "MoogsoftTarget___get_incident_by_id",
            ),
            call_tool=lambda _name: {"incident": {"id": "1"}, "logs": {"results": []}},
            configured=True,
            assume_endpoint=True,
        )
        assert [(row["worker"], row["tool"]) for row in report["tools"]] == [
            ("log_worker", "splunk.get_change_data"),
            ("log_worker", "splunk.search_oneshot"),
            ("ops_worker", "moogsoft.get_incident_by_id"),
        ]
        assert [row["worker"] for row in report["workers"]] == ["log_worker", "ops_worker"]
        text = format_text(report)
        assert text.index("log_worker") < text.index("splunk.get_change_data")
        assert text.index("splunk.get_change_data") < text.index("splunk.search_oneshot")
        assert text.index("splunk.search_oneshot") < text.index("ops_worker")

    def test_exit_code_follows_required_workers_only(self) -> None:
        healthy = diagnose(
            workers={"ops_worker": frozenset({"moogsoft"})},
            tools_for={"ops_worker": ("moogsoft.get_incident_by_id",)},
            list_tools=lambda: ["MoogsoftTarget___get_incident_by_id"],
            call_tool=lambda _name: {"incident": {"id": "1"}},
            configured=True,
            assume_endpoint=True,
        )
        assert healthy["workers"][0]["state"] == "reachable"
        assert report_exit_code(healthy) == 0

        missing = diagnose(
            workers={"itsm_worker": frozenset({"servicenow"})},
            tools_for={"itsm_worker": ("servicenow.get_ci_details",)},
            list_tools=lambda: [],
            call_tool=lambda _name: {},
            configured=True,
            assume_endpoint=True,
        )
        assert missing["workers"][0]["state"] == "missing"
        assert report_exit_code(missing) == 2

    def test_log_worker_search_reachable_change_data_skipped(self) -> None:
        """Live OSS shape: search hits Loki, change data is an honest skip."""
        report = diagnose(
            workers={"log_worker": frozenset({"splunk"})},
            tools_for={"log_worker": ("splunk.search_oneshot", "splunk.get_change_data")},
            list_tools=lambda: _listed(
                "SplunkTarget___search_oneshot",
                "SplunkTarget___get_change_data",
            ),
            call_tool=lambda name: (
                {"skipped": True, "changes": [], "change_data": []}
                if "get_change_data" in name
                else {"logs": {"results": [{"message": "timeout"}], "count": 1}}
            ),
            configured=True,
            assume_endpoint=True,
        )
        by_tool = {row["tool"]: row for row in report["tools"]}
        search = by_tool["splunk.search_oneshot"]
        changes = by_tool["splunk.get_change_data"]
        assert search["state"] == "reachable"
        assert search["required"] is True
        assert search["worker"] == "log_worker"
        assert changes["state"] == "missing"
        assert changes["required"] is True
        assert changes["detail"] == "not_provided"
        assert report["workers"][0]["state"] == "missing"
        assert "not_provided" in report["workers"][0]["reason"]
        assert "splunk.get_change_data" in report["workers"][0]["reason"]
        assert report_exit_code(report) == 2
        text = format_text(report)
        assert "splunk.search_oneshot\treachable" in text
        assert "splunk.get_change_data\tmissing" in text


class TestReplayHashUnchanged:
    def test_investigation_hash_ignores_the_new_tools_key(self) -> None:
        result = {
            "incident_id": "INC12345",
            "root_cause": "example",
            "confidence": 47,
            "evidence_timeline": [],
        }
        bare = replay_hash(result, None)
        older = {"gateway_mode": "stub", "plain_mcp": False, "workers": []}
        newer = {**older, "tools": [{"worker": "log_worker", "tool": "splunk.search_oneshot"}]}
        assert replay_hash(result, None) == bare
        assert replay_hash(result, older) != replay_hash(result, newer)
