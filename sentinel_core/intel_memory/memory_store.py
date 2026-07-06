"""JSON-per-record MemoryStore.

Persistent memory for :class:`MemoryRecord` objects. Deterministic
JSON writes (sort_keys=True). Never touches production paths — caller
supplies the root directory.
"""
from __future__ import annotations

import json
from pathlib import Path

from sentinel_core.intel_memory.schemas import MemoryRecord


class MemoryStoreError(RuntimeError):
    """Raised on deterministic store failures."""


class MemoryStore:
    """One JSON file per :class:`MemoryRecord`, keyed by ``memory_id``."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, record: MemoryRecord) -> Path:
        if not record.memory_id:
            raise MemoryStoreError("MemoryRecord requires a memory_id")
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path_for(record.memory_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(record.to_dict(), sort_keys=True, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    def load(self, memory_id: str) -> MemoryRecord:
        path = self._path_for(memory_id)
        if not path.exists():
            raise MemoryStoreError(f"memory '{memory_id}' not found at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(f"memory '{memory_id}' invalid JSON: {exc}") from exc
        return MemoryRecord.from_dict(raw)

    def list_ids(self) -> tuple[str, ...]:
        if not self._root.exists():
            return ()
        return tuple(sorted(p.stem for p in self._root.glob("*.json")))

    def load_all(self) -> tuple[MemoryRecord, ...]:
        return tuple(self.load(mid) for mid in self.list_ids())

    def has(self, memory_id: str) -> bool:
        return self._path_for(memory_id).exists()

    def delete(self, memory_id: str) -> None:
        """Remove ``memory_id`` from the primary namespace, archiving the
        underlying record for auditability.

        RC-E: previously ``delete`` unlinked the JSON file, destroying
        audit history. The public contract is unchanged — after this
        call, ``has(memory_id)`` returns False, ``load(memory_id)``
        raises, and ``list_ids()`` no longer enumerates the id — but
        the record is preserved under ``{root}/.deleted/`` and can be
        recovered via :meth:`list_deleted` / :meth:`load_deleted`.
        """
        p = self._path_for(memory_id)
        if not p.exists():
            return
        trash = self._root / ".deleted"
        trash.mkdir(parents=True, exist_ok=True)
        dst = trash / f"{memory_id}.json"
        # If a prior tombstone exists for the same id, append a counter
        # so no historical version is ever lost.
        if dst.exists():
            i = 1
            while (trash / f"{memory_id}.{i}.json").exists():
                i += 1
            dst = trash / f"{memory_id}.{i}.json"
        p.rename(dst)

    def list_deleted(self) -> tuple[str, ...]:
        """Return the file stems currently in ``.deleted/`` (audit trail).

        Additive companion to :meth:`delete`. Deterministic (sorted)
        just like :meth:`list_ids`. Returns an empty tuple when no
        record has ever been deleted.
        """
        trash = self._root / ".deleted"
        if not trash.exists():
            return ()
        return tuple(sorted(p.stem for p in trash.glob("*.json")))

    def load_deleted(self, stem: str) -> MemoryRecord:
        """Load a previously-deleted record by its ``.deleted/`` stem.

        The stem is the filename without the ``.json`` extension —
        typically the ``memory_id`` for the first deletion or
        ``{memory_id}.{N}`` for subsequent ones.
        """
        trash = self._root / ".deleted"
        path = trash / f"{stem}.json"
        if not path.exists():
            raise MemoryStoreError(
                f"deleted record '{stem}' not found at {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryStoreError(
                f"deleted record '{stem}' invalid JSON: {exc}"
            ) from exc
        return MemoryRecord.from_dict(raw)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _path_for(self, memory_id: str) -> Path:
        if not memory_id or "/" in memory_id or "\\" in memory_id:
            raise MemoryStoreError(f"invalid memory_id: {memory_id!r}")
        return self._root / f"{memory_id}.json"


__all__ = [
    "MemoryStore",
    "MemoryStoreError",
]
