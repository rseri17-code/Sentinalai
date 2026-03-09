"""Tests for AgentCore runtime adapter."""


from agentcore_runtime import _handle_invocation


class TestHandleInvocation:
    def test_missing_incident_id(self):
        result = _handle_invocation({})
        assert "error" in result

    def test_investigate_via_incident_id(self):
        result = _handle_invocation({"incident_id": "INC12345"})
        assert "result" in result
        assert result["incident_id"] == "INC12345"
        rca = result["result"]
        assert "root_cause" in rca
        assert "confidence" in rca
        assert rca["confidence"] > 0

    def test_investigate_via_prompt_field(self):
        """AgentCore SDK may pass input as 'prompt' field."""
        result = _handle_invocation({"prompt": "INC12345"})
        assert "result" in result
        assert result["incident_id"] == "INC12345"

    def test_all_incidents_return_results(self):
        """Verify all 10 test incidents produce valid RCA through AgentCore."""
        incident_ids = [
            "INC12345", "INC12346", "INC12347", "INC12348", "INC12349",
            "INC12350", "INC12351", "INC12352", "INC12353", "INC12354",
        ]
        for iid in incident_ids:
            result = _handle_invocation({"incident_id": iid})
            assert "result" in result, f"No result for {iid}"
            rca = result["result"]
            assert rca["confidence"] > 0, f"Zero confidence for {iid}"
            assert rca["root_cause"], f"Empty root cause for {iid}"
            assert rca["reasoning"], f"Empty reasoning for {iid}"

    def test_replay_mode(self, tmp_path, monkeypatch):
        """Test that replay mode works through the handler."""
        monkeypatch.setenv("SENTINALAI_REPLAY_DIR", str(tmp_path))
        # First call stores the artifact
        result1 = _handle_invocation({"incident_id": "INC12345"})
        assert result1["result"]["confidence"] > 0

        # Second call with replay=True should return stored result
        result2 = _handle_invocation({"incident_id": "INC12345", "replay": True})
        assert result2["result"]["root_cause"] == result1["result"]["root_cause"]
