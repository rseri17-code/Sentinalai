"""Tests for ReplayStore retention/TTL cleanup."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _write_artifact(replay_dir: Path, case_id: str, age_seconds: float = 0) -> Path:
    ts = "20240101T000000Z"
    p = replay_dir / f"{case_id}_{ts}.json"
    p.write_text(json.dumps({"case_id": case_id, "receipts": [], "result": {}}))
    if age_seconds:
        mtime = time.time() - age_seconds
        import os
        os.utime(p, (mtime, mtime))
    return p


class TestReplayStorePurge:
    def test_purge_deletes_old_files(self, tmp_path):
        from supervisor.replay import ReplayStore

        store = ReplayStore(str(tmp_path))
        old = _write_artifact(tmp_path, "OLD001", age_seconds=48 * 3600)  # 48h old
        new = _write_artifact(tmp_path, "NEW001", age_seconds=0)

        deleted = store._purge_old_artifacts(max_age_hours=24, max_files=1000)

        assert deleted == 1
        assert not old.exists()
        assert new.exists()

    def test_purge_enforces_max_file_count(self, tmp_path):
        from supervisor.replay import ReplayStore

        store = ReplayStore(str(tmp_path))
        for i in range(10):
            _write_artifact(tmp_path, f"INC{i:04d}", age_seconds=i)  # different mtimes

        deleted = store._purge_old_artifacts(max_age_hours=999, max_files=5)

        assert deleted == 5
        remaining = list(tmp_path.glob("*.json"))
        assert len(remaining) == 5

    def test_purge_called_on_save(self, tmp_path):
        from supervisor.replay import ReplayStore

        store = ReplayStore(str(tmp_path))
        # Write 6 old files directly
        for i in range(6):
            _write_artifact(tmp_path, f"OLD{i:03d}", age_seconds=48 * 3600)

        # save() should trigger purge
        store.save("NEW001", receipts=[], result={"root_cause": "test"})

        remaining = list(tmp_path.glob("*.json"))
        # Default max_age=24h so old ones should be gone; NEW001 stays
        old_remaining = [f for f in remaining if "OLD" in f.name]
        assert len(old_remaining) == 0, f"Old files not purged: {old_remaining}"

    def test_purge_noop_when_dir_missing(self, tmp_path):
        from supervisor.replay import ReplayStore

        store = ReplayStore(str(tmp_path / "nonexistent"))
        deleted = store._purge_old_artifacts(max_age_hours=1, max_files=5)
        assert deleted == 0

    def test_purge_keeps_newest_on_count_limit(self, tmp_path):
        from supervisor.replay import ReplayStore
        import os

        store = ReplayStore(str(tmp_path))
        files = []
        for i in range(5):
            f = _write_artifact(tmp_path, f"INC{i:04d}", age_seconds=(5 - i) * 10)
            files.append(f)

        # The newest file should survive
        newest = files[-1]  # age_seconds=10 (most recent)
        deleted = store._purge_old_artifacts(max_age_hours=999, max_files=1)
        assert newest.exists(), "Newest file should be kept"
        assert deleted == 4
