"""JSONL-backed graph storage for institutional knowledge.

Lightweight, file-based backend that stores nodes and edges as JSONL records.
No external database dependencies. Designed for single-process use with
eventual migration path to Neptune/OpenSearch.

Storage layout:
    {storage_dir}/nodes.jsonl  — one JSON object per line (node records)
    {storage_dir}/edges.jsonl  — one JSON object per line (edge records)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("sentinalai.knowledge.graph_backend")


class GraphBackendJson:
    """JSONL file-backed graph storage."""

    def __init__(self, storage_dir: str):
        self._storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)
        self._nodes_path = os.path.join(storage_dir, "nodes.jsonl")
        self._edges_path = os.path.join(storage_dir, "edges.jsonl")

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #

    def upsert_node(
        self,
        node_type: str,
        node_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """Insert or update a node. Deduplicates by node_id."""
        record = {
            "node_type": node_type,
            "node_id": node_id,
            "metadata": metadata,
            "timestamp": time.time(),
        }

        # Read existing, filter out old version of same node_id
        existing = self._read_jsonl(self._nodes_path)
        updated = [n for n in existing if n.get("node_id") != node_id]
        updated.append(record)
        self._write_jsonl(self._nodes_path, updated)

    def get_nodes(
        self,
        node_type: str,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Get nodes by type, optionally filtered by metadata fields."""
        all_nodes = self._read_jsonl(self._nodes_path)
        results = [n for n in all_nodes if n.get("node_type") == node_type]

        if metadata_filter:
            filtered = []
            for node in results:
                meta = node.get("metadata", {})
                if all(meta.get(k) == v for k, v in metadata_filter.items()):
                    filtered.append(node)
            results = filtered

        return results

    # ------------------------------------------------------------------ #
    # Edges
    # ------------------------------------------------------------------ #

    def add_edge(
        self,
        source: str,
        relationship: str,
        target: str,
        weight: float = 1.0,
    ) -> None:
        """Add a directed edge between two nodes."""
        record = {
            "source": source,
            "relationship": relationship,
            "target": target,
            "weight": weight,
            "timestamp": time.time(),
        }
        existing = self._read_jsonl(self._edges_path)
        existing.append(record)
        self._write_jsonl(self._edges_path, existing)

    def get_edges(
        self,
        source: str | None = None,
        target: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get edges, optionally filtered by source and/or target."""
        all_edges = self._read_jsonl(self._edges_path)
        results = all_edges
        if source is not None:
            results = [e for e in results if e.get("source") == source]
        if target is not None:
            results = [e for e in results if e.get("target") == target]
        return results

    # ------------------------------------------------------------------ #
    # JSONL I/O
    # ------------------------------------------------------------------ #

    def _read_jsonl(self, path: str) -> list[dict]:
        if not os.path.exists(path):
            return []
        records = []
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed JSONL line in %s", path)
        return records

    def _write_jsonl(self, path: str, records: list[dict]) -> None:
        with open(path, "w") as f:
            for record in records:
                f.write(json.dumps(record, default=str) + "\n")
