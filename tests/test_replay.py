"""Tests for investigation replay system."""

from pathlib import Path


from supervisor.replay import ReplayStore, replay_investigation


class TestReplayStore:
    def test_save_and_load(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        receipts = [{"tool": "ops", "action": "get", "status": "success"}]
        result = {"root_cause": "timeout", "confidence": 92}

        path = store.save("INC001", receipts, result)
        assert Path(path).exists()

        loaded = store.load("INC001")
        assert loaded is not None
        assert loaded["case_id"] == "INC001"
        assert loaded["result"]["root_cause"] == "timeout"
        assert loaded["receipts"] == receipts

    def test_load_nonexistent(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        assert store.load("INC_MISSING") is None

    def test_load_from_empty_dir(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        assert store.load("INC1") is None

    def test_load_from_missing_dir(self):
        store = ReplayStore("/tmp/sentinalai_test_nonexistent_dir_xyz")
        assert store.load("INC1") is None

    def test_list_cases(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        store.save("INC001", [], {})
        store.save("INC002", [], {})
        store.save("INC001", [], {})  # Second save for same case

        cases = store.list_cases()
        assert "INC001" in cases
        assert "INC002" in cases

    def test_saves_evidence(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        evidence = {"logs": [{"msg": "timeout"}]}
        store.save("INC003", [], {"confidence": 80}, evidence=evidence)

        loaded = store.load("INC003")
        assert loaded["evidence"] == evidence


    def test_load_corrupted_json_returns_none(self, tmp_path):
        """Corrupted JSON file must return None, not raise."""
        store = ReplayStore(str(tmp_path))
        # Write a valid-looking filename but with invalid JSON content
        bad_file = tmp_path / "INC_BAD_20240101T000000Z.json"
        bad_file.write_text("not valid json {{{")

        result = store.load("INC_BAD")
        assert result is None

    def test_list_cases_missing_dir_returns_empty(self):
        """list_cases on nonexistent dir must return empty list."""
        store = ReplayStore("/tmp/sentinalai_test_nonexistent_dir_abc123")
        assert store.list_cases() == []


class TestReplayInvestigation:
    def test_replay_returns_stored_result(self, tmp_path):
        store = ReplayStore(str(tmp_path))
        result = {"root_cause": "memory leak", "confidence": 88}
        store.save("INC005", [], result)

        replayed = replay_investigation("INC005", str(tmp_path))
        assert replayed == result

    def test_replay_returns_none_for_missing(self, tmp_path):
        replayed = replay_investigation("INC_MISSING", str(tmp_path))
        assert replayed is None
