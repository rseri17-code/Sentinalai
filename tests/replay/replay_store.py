"""ReplayStore — persistence for benchmark runs.

Reads/writes ``BenchmarkRun`` records as JSON files under a caller-
supplied directory (never a hard-coded production path). Every write
uses ``sort_keys=True`` for byte-deterministic output.

Zero side effects at import time. Zero I/O on default construction.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

from tests.replay.schemas import BenchmarkRun


class ReplayStoreError(RuntimeError):
    """Raised when a store operation fails deterministically."""


class ReplayStore:
    """JSON-per-run store keyed by ``run_id``.

    All paths are relative to ``root``. Runs are listed / iterated in
    lexicographic order of ``run_id`` so the store is CI-deterministic.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, run: BenchmarkRun) -> Path:
        """Persist ``run`` under ``{root}/{run_id}.json``.

        RC-E: previously ``save`` unconditionally overwrote the primary
        file, silently destroying any prior version for the same
        ``run_id``. Now, if a prior version exists at the primary path,
        it is atomically moved to ``{root}/history/{run_id}.{prior_generated_at}.json``
        (with an integer suffix on collision) before the new record is
        written. The primary path continues to hold the latest version
        — every existing reader (``load``, ``load_all``, ``has``,
        ``list_runs``) is unaffected.

        Concurrency: the temporary write path now includes PID + UUID
        so parallel CI writers do not race on a shared ``.tmp`` name.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(run.run_id)
        # Archive prior version — append-only history preservation.
        if path.exists():
            history_dir = self._root / "history"
            history_dir.mkdir(parents=True, exist_ok=True)
            try:
                prior = self.load(run.run_id)
                prior_stamp = str(prior.generated_at) or "unknown"
            except ReplayStoreError:
                prior_stamp = "unknown"
            hist_path = history_dir / f"{run.run_id}.{prior_stamp}.json"
            if hist_path.exists():
                i = 1
                while (
                    history_dir / f"{run.run_id}.{prior_stamp}.{i}.json"
                ).exists():
                    i += 1
                hist_path = (
                    history_dir / f"{run.run_id}.{prior_stamp}.{i}.json"
                )
            path.rename(hist_path)
        # RC-E: unique tmp filename eliminates concurrent-writer races.
        tmp = path.with_suffix(
            f"{path.suffix}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
        )
        tmp.write_text(json.dumps(run.to_dict(), sort_keys=True, indent=2),
                        encoding="utf-8")
        tmp.replace(path)
        return path

    def load(self, run_id: str) -> BenchmarkRun:
        path = self._path_for(run_id)
        if not path.exists():
            raise ReplayStoreError(f"run '{run_id}' not found at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReplayStoreError(f"run '{run_id}' invalid JSON: {exc}") from exc
        return BenchmarkRun.from_dict(raw)

    def list_runs(self) -> tuple[str, ...]:
        if not self._root.exists():
            return ()
        return tuple(sorted(p.stem for p in self._root.glob("*.json")))

    def load_all(self) -> tuple[BenchmarkRun, ...]:
        return tuple(self.load(rid) for rid in self.list_runs())

    def has(self, run_id: str) -> bool:
        return self._path_for(run_id).exists()

    def load_history(self, run_id: str) -> tuple[BenchmarkRun, ...]:
        """Return every archived prior version of ``run_id``.

        Additive companion to :meth:`save`'s new history-preservation
        behavior. Returned tuple is sorted by ``generated_at`` (the
        stem's second component), oldest first. Empty tuple when no
        prior versions have been archived.
        """
        history_dir = self._root / "history"
        if not history_dir.exists():
            return ()
        candidates = sorted(history_dir.glob(f"{run_id}.*.json"))
        out: list[BenchmarkRun] = []
        for path in candidates:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Malformed archive entries are skipped — we do not
                # break the caller's audit walk on one bad file.
                continue
            out.append(BenchmarkRun.from_dict(raw))
        return tuple(out)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def load_in_date_range(self, start_iso: str, end_iso: str) -> tuple[BenchmarkRun, ...]:
        """Return runs whose ``generated_at`` is within ``[start, end]``.

        Comparison is lexicographic on ISO 8601 timestamps — safe for
        UTC ISO strings.
        """
        s, e = str(start_iso or ""), str(end_iso or "")
        return tuple(r for r in self.load_all()
                       if (not s or r.generated_at >= s)
                       and (not e or r.generated_at <= e))

    def load_by_service(self, service: str) -> tuple[BenchmarkRun, ...]:
        """Return runs whose metadata contains service=…. Deterministic."""
        svc = str(service or "")
        return tuple(r for r in self.load_all()
                       if str(r.metadata.get("service", "")) == svc)

    def load_by_incident_type(self, incident_type: str) -> tuple[BenchmarkRun, ...]:
        it = str(incident_type or "")
        return tuple(r for r in self.load_all()
                       if str(r.metadata.get("incident_type", "")) == it)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path_for(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or "\\" in run_id:
            raise ReplayStoreError(f"invalid run_id: {run_id!r}")
        return self._root / f"{run_id}.json"


__all__ = [
    "ReplayStore",
    "ReplayStoreError",
]
