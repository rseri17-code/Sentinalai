"""LLM call graph — trace every LLM call into a parent-child DAG.

Every LLM call made during an investigation is recorded as a node in a
directed acyclic graph (DAG).  Each node stores:
  - call_id (UUID)
  - parent_id (UUID of calling context, or None for root)
  - purpose   (what this call was for: "rca_analysis", "self_critique", etc.)
  - model     (claude-sonnet-4-6, etc.)
  - input_tokens / output_tokens / latency_ms
  - prompt_hash (SHA-256 of first 200 chars of prompt — for dedup detection)
  - timestamp

Use cases:
  - Debug runaway token consumption: which call chain is expensive?
  - Detect prompt loops: same prompt_hash called multiple times?
  - Visualise the investigation reasoning chain in the AG UI
  - Feed regression harness with per-call-type latency trends

Thread-local context: each investigation gets its own CallGraph instance,
stored in the supervisor's thread-local storage.  Thread-safe across
concurrent investigations.

Usage:
    from supervisor.llm_call_graph import CallGraph, LLMCallNode, current_graph

    graph = CallGraph(investigation_id="inv-123")
    with graph.span("rca_analysis") as call_id:
        # make LLM call
        graph.record(call_id, model="claude-sonnet-4-6",
                     input_tokens=1200, output_tokens=400, latency_ms=2100)
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from threading import local
from typing import Any, Generator

logger = logging.getLogger("sentinalai.llm_call_graph")

# Thread-local current graph (set by the supervisor per investigation)
_tls = local()


def current_graph() -> "CallGraph | None":
    """Return the active call graph for the current thread."""
    return getattr(_tls, "graph", None)


def set_current_graph(graph: "CallGraph | None") -> None:
    """Bind a call graph to the current thread."""
    _tls.graph = graph


@dataclass
class LLMCallNode:
    """Single LLM call in the trace graph."""

    call_id: str
    parent_id: str | None
    purpose: str                    # rca_analysis | self_critique | llm_judge | memory | etc.
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    prompt_hash: str = ""           # SHA-256 prefix for dedup detection
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    error: str = ""                 # non-empty if LLM call raised

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def duration_ms(self) -> float:
        if self.finished_at > 0:
            return round((self.finished_at - self.started_at) * 1000, 1)
        return self.latency_ms

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        d["duration_ms"] = self.duration_ms
        return d


class CallGraph:
    """DAG of LLM calls for a single investigation.

    Thread-safe for concurrent span creation (each investigation has its
    own instance, but multiple threads may record to the same graph if
    workers run in a thread pool).
    """

    def __init__(self, investigation_id: str) -> None:
        self.investigation_id = investigation_id
        self._nodes: dict[str, LLMCallNode] = {}
        self._children: dict[str | None, list[str]] = {}  # parent_id → [child_ids]
        self._active_span_stack: list[str] = []  # for nested spans
        import threading
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Span context manager
    # ------------------------------------------------------------------ #

    @contextmanager
    def span(self, purpose: str, prompt_prefix: str = "") -> Generator[str, None, None]:
        """Context manager that creates a span node and tracks nesting.

        Usage:
            with graph.span("rca_analysis", prompt_prefix=prompt[:200]) as call_id:
                result = llm_call(...)
                graph.record(call_id, model=..., input_tokens=..., ...)
        """
        call_id = str(uuid.uuid4())
        with self._lock:
            parent_id = self._active_span_stack[-1] if self._active_span_stack else None
            node = LLMCallNode(
                call_id=call_id,
                parent_id=parent_id,
                purpose=purpose,
                prompt_hash=_hash_prompt(prompt_prefix) if prompt_prefix else "",
                started_at=time.time(),
            )
            self._nodes[call_id] = node
            self._children.setdefault(parent_id, []).append(call_id)
            self._active_span_stack.append(call_id)

        try:
            yield call_id
        except Exception as exc:
            with self._lock:
                if call_id in self._nodes:
                    self._nodes[call_id].error = str(exc)[:200]
            raise
        finally:
            with self._lock:
                if call_id in self._active_span_stack:
                    self._active_span_stack.remove(call_id)
                if call_id in self._nodes:
                    self._nodes[call_id].finished_at = time.time()

    def record(
        self,
        call_id: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        """Record completion metrics for a span."""
        with self._lock:
            node = self._nodes.get(call_id)
            if node is None:
                return
            node.model = model
            node.input_tokens = input_tokens
            node.output_tokens = output_tokens
            node.latency_ms = latency_ms
            if node.finished_at == 0.0:
                node.finished_at = time.time()

    # ------------------------------------------------------------------ #
    # Query API
    # ------------------------------------------------------------------ #

    def get_node(self, call_id: str) -> LLMCallNode | None:
        return self._nodes.get(call_id)

    def root_nodes(self) -> list[LLMCallNode]:
        """Return all top-level (parentless) nodes."""
        return [n for n in self._nodes.values() if n.parent_id is None]

    def children_of(self, call_id: str) -> list[LLMCallNode]:
        child_ids = self._children.get(call_id, [])
        return [self._nodes[cid] for cid in child_ids if cid in self._nodes]

    def all_nodes(self) -> list[LLMCallNode]:
        return list(self._nodes.values())

    def total_tokens(self) -> int:
        return sum(n.total_tokens for n in self._nodes.values())

    def total_cost_estimate_usd(
        self, input_cost_per_mtok: float = 3.0, output_cost_per_mtok: float = 15.0
    ) -> float:
        """Rough cost estimate using Claude Sonnet pricing (per million tokens)."""
        input_toks = sum(n.input_tokens for n in self._nodes.values())
        output_toks = sum(n.output_tokens for n in self._nodes.values())
        return round(
            (input_toks / 1_000_000) * input_cost_per_mtok
            + (output_toks / 1_000_000) * output_cost_per_mtok,
            6,
        )

    def duplicate_calls(self) -> list[str]:
        """Return call_ids where the same prompt was submitted twice."""
        seen: dict[str, str] = {}
        dupes: list[str] = []
        for node in self._nodes.values():
            if not node.prompt_hash:
                continue
            if node.prompt_hash in seen:
                dupes.append(node.call_id)
            else:
                seen[node.prompt_hash] = node.call_id
        return dupes

    def summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary of the call graph."""
        nodes = self.all_nodes()
        by_purpose: dict[str, dict] = {}
        for n in nodes:
            if n.purpose not in by_purpose:
                by_purpose[n.purpose] = {"count": 0, "input_tokens": 0, "output_tokens": 0, "total_latency_ms": 0.0}
            by_purpose[n.purpose]["count"] += 1
            by_purpose[n.purpose]["input_tokens"] += n.input_tokens
            by_purpose[n.purpose]["output_tokens"] += n.output_tokens
            by_purpose[n.purpose]["total_latency_ms"] += n.duration_ms

        return {
            "investigation_id": self.investigation_id,
            "total_calls": len(nodes),
            "total_tokens": self.total_tokens(),
            "estimated_cost_usd": self.total_cost_estimate_usd(),
            "duplicate_calls": len(self.duplicate_calls()),
            "by_purpose": by_purpose,
            "nodes": [n.to_dict() for n in nodes],
        }

    def to_dict(self) -> dict[str, Any]:
        return self.summary()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_prompt(text: str) -> str:
    """Short SHA-256 hash of the first 200 chars of a prompt."""
    return hashlib.sha256(text[:200].encode()).hexdigest()[:16]
