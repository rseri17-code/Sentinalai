"""Tool call transparency layer for SentinalAI.

Captures 4 layers of information per tool call:
  1. Intent   — why was this called (phase, worker, action context)
  2. The Call — exact params sent, timing
  3. Response — raw result + extracted signal vs noise
  4. Influence — hypothesis confidence delta before/after

Thread-safe; keyed by investigation_id so concurrent investigations
don't contaminate each other.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class SignalFact:
    """A single extracted signal from a tool response."""
    category: str    # error | anomaly | change | threshold | service | generic
    text: str
    weight: float = 1.0  # 0.0–1.0 relative importance


@dataclass
class HypothesisDelta:
    name: str
    score_before: float
    score_after: float

    @property
    def delta(self) -> float:
        return self.score_after - self.score_before


@dataclass
class EnrichedToolReceipt:
    """Full 4-layer transparency record for one tool invocation."""

    receipt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    investigation_id: str = ""

    # Layer 1 – Intent
    phase: str = ""          # collect | analyze | classify | playbook
    worker: str = ""
    action: str = ""
    intent_summary: str = "" # human-readable why (generated from worker+action+params)

    # Layer 2 – The Call
    params: dict = field(default_factory=dict)
    called_at_ms: float = 0.0   # epoch ms
    latency_ms: float = 0.0
    status: str = "success"     # success | error | timeout

    # Layer 3 – Response
    result_count: int = 0
    signal_facts: list[SignalFact] = field(default_factory=list)
    noise_ratio: float = 0.0   # 0.0 = all signal, 1.0 = all noise
    raw_preview: str = ""       # first 500 chars of result for quick scan
    error_msg: str = ""

    # Layer 4 – Influence
    hypothesis_deltas: list[HypothesisDelta] = field(default_factory=list)
    confidence_before: float = 0.0
    confidence_after: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @property
    def confidence_delta(self) -> float:
        return self.confidence_after - self.confidence_before

    @property
    def signal_count(self) -> int:
        return len(self.signal_facts)


# ── Signal extraction ─────────────────────────────────────────────────────────

_ERROR_PATTERNS = re.compile(
    r'\b(error|exception|fail|crash|oom|killed|timeout|503|500|502|504|panic)\b',
    re.IGNORECASE,
)
_ANOMALY_PATTERNS = re.compile(
    r'\b(spike|surge|drop|latency|p99|p95|elevated|degraded|abnormal|outlier)\b',
    re.IGNORECASE,
)
_CHANGE_PATTERNS = re.compile(
    r'\b(deploy|rollout|config|migration|release|restart|scale|update|patch)\b',
    re.IGNORECASE,
)
_THRESHOLD_PATTERNS = re.compile(
    r'\b(\d+\.?\d*\s*%|threshold|alert|alarm|breach|violat)\b',
    re.IGNORECASE,
)


def _extract_signals_from_result(result: Any, worker: str, action: str) -> tuple[list[SignalFact], float]:
    """Return (signal_facts, noise_ratio) from a tool result dict."""
    if not result or not isinstance(result, dict):
        return [], 1.0

    facts: list[SignalFact] = []
    total_items = 0

    # -- Logs
    logs = result.get("logs") or result.get("log_lines") or []
    if isinstance(logs, list):
        for entry in logs[:200]:
            text = str(entry.get("message") or entry.get("msg") or entry) if isinstance(entry, dict) else str(entry)
            if _ERROR_PATTERNS.search(text):
                facts.append(SignalFact("error", text[:120], 1.0))
            elif _ANOMALY_PATTERNS.search(text):
                facts.append(SignalFact("anomaly", text[:120], 0.8))
            total_items += 1

    # -- Metrics
    metrics = result.get("metrics") or {}
    if isinstance(metrics, dict):
        for k, v in metrics.items():
            text = f"{k}={v}"
            if _THRESHOLD_PATTERNS.search(text) or _ANOMALY_PATTERNS.search(text):
                facts.append(SignalFact("threshold", text[:120], 0.9))
            total_items += 1

    # -- Events / changes
    events = result.get("events") or result.get("changes") or []
    if isinstance(events, list):
        for ev in events[:50]:
            text = str(ev.get("description") or ev.get("message") or ev) if isinstance(ev, dict) else str(ev)
            if _CHANGE_PATTERNS.search(text):
                facts.append(SignalFact("change", text[:120], 0.85))
            total_items += 1

    # -- Errors in result itself
    if result.get("error"):
        facts.append(SignalFact("error", str(result["error"])[:200], 1.0))

    # -- Similar incidents (memory match — high signal)
    similar = result.get("similar_incidents") or result.get("results") or []
    if isinstance(similar, list) and worker in ("memory", "knowledge"):
        for item in similar[:5]:
            summary = str(item.get("summary") or item.get("incident_id") or item)[:120]
            facts.append(SignalFact("service", summary, 0.75))

    signal_ct = len(facts)
    noise_ratio = max(0.0, 1.0 - (signal_ct / max(total_items, 1))) if total_items > 0 else (0.0 if signal_ct else 1.0)

    return facts[:30], round(noise_ratio, 2)


def _make_intent_summary(phase: str, worker: str, action: str, params: dict) -> str:
    """Generate a one-line human-readable intent string."""
    service = params.get("service") or params.get("service_name") or ""
    incident = params.get("incident_id") or params.get("query") or ""
    if phase == "collect":
        parts = [f"Collect {action}"]
        if service:
            parts.append(f"for {service}")
        if incident:
            parts.append(f"({str(incident)[:40]})")
        return " ".join(parts)
    if phase == "analyze":
        return f"Analyze: {worker}.{action}"
    if phase == "classify":
        return f"Classify incident via {worker}"
    return f"{worker}.{action}"


def _count_results(result: dict | None) -> int:
    if not result or not isinstance(result, dict):
        return 0
    for key in ("results", "events", "changes", "metrics", "similar_incidents", "log_lines", "logs"):
        val = result.get(key)
        if isinstance(val, list):
            return len(val)
        if isinstance(val, dict):
            inner = val.get("results") or val.get("metrics")
            if isinstance(inner, list):
                return len(inner)
    return 1 if result else 0


def _raw_preview(result: Any) -> str:
    if not result:
        return ""
    try:
        import json
        s = json.dumps(result, default=str)
        return s[:500]
    except Exception:
        return str(result)[:500]


# ── Per-investigation store ────────────────────────────────────────────────────

class _InvestigationTransparency:
    """Thread-safe ordered receipt store for one investigation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._receipts: list[EnrichedToolReceipt] = []
        # snapshot of hypothesis scores set by _analyze_evidence before LLM
        self._pre_llm_scores: dict[str, float] = {}

    def add(self, receipt: EnrichedToolReceipt) -> None:
        with self._lock:
            self._receipts.append(receipt)

    def set_pre_llm_scores(self, scores: dict[str, float]) -> None:
        with self._lock:
            self._pre_llm_scores = dict(scores)

    def get_pre_llm_scores(self) -> dict[str, float]:
        with self._lock:
            return dict(self._pre_llm_scores)

    def all(self) -> list[EnrichedToolReceipt]:
        with self._lock:
            return list(self._receipts)

    def get(self, receipt_id: str) -> EnrichedToolReceipt | None:
        with self._lock:
            for r in self._receipts:
                if r.receipt_id == receipt_id:
                    return r
            return None


# ── Global emitter ────────────────────────────────────────────────────────────

class ToolTransparencyEmitter:
    """Process-singleton that manages transparency records across investigations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stores: dict[str, _InvestigationTransparency] = defaultdict(_InvestigationTransparency)

    def _store(self, investigation_id: str) -> _InvestigationTransparency:
        with self._lock:
            return self._stores[investigation_id]

    def record_call_result(
        self,
        *,
        investigation_id: str,
        worker: str,
        action: str,
        params: dict,
        raw_response: Any,
        latency_ms: float,
        status: str,
        phase: str = "collect",
        error_msg: str = "",
    ) -> EnrichedToolReceipt:
        signal_facts, noise_ratio = _extract_signals_from_result(raw_response, worker, action)
        receipt = EnrichedToolReceipt(
            investigation_id=investigation_id,
            phase=phase,
            worker=worker,
            action=action,
            intent_summary=_make_intent_summary(phase, worker, action, params),
            params={k: v for k, v in params.items() if k not in ("api_key", "token", "password")},
            called_at_ms=time.time() * 1000,
            latency_ms=round(latency_ms, 1),
            status=status,
            result_count=_count_results(raw_response),
            signal_facts=signal_facts,
            noise_ratio=noise_ratio,
            raw_preview=_raw_preview(raw_response) if status == "success" else "",
            error_msg=error_msg,
        )
        self._store(investigation_id).add(receipt)
        # Persist asynchronously — never block the investigation
        try:
            from database.ops_persistence import get_ops_store
            get_ops_store().persist_receipt(receipt)
        except Exception:
            pass
        return receipt

    def record_pre_llm_scores(self, investigation_id: str, hypotheses: list) -> None:
        """Snapshot hypothesis scores before LLM refinement."""
        scores = {h.name: float(h.base_score) for h in hypotheses}
        self._store(investigation_id).set_pre_llm_scores(scores)

    def record_post_llm_scores(
        self, investigation_id: str, hypotheses: list, confidence_before: float, confidence_after: float
    ) -> None:
        """Attach hypothesis deltas to the most recent LLM call record, if any."""
        store = self._store(investigation_id)
        pre = store.get_pre_llm_scores()
        deltas = []
        for h in hypotheses:
            before = pre.get(h.name, float(h.base_score))
            deltas.append(HypothesisDelta(
                name=h.name,
                score_before=before,
                score_after=float(h.base_score),
            ))

        # Attach to the last receipt that looks like an LLM analysis call
        receipts = store.all()
        for r in reversed(receipts):
            if r.phase == "analyze":
                r.hypothesis_deltas = deltas
                r.confidence_before = confidence_before
                r.confidence_after = confidence_after
                break

    def get_receipts(self, investigation_id: str) -> list[EnrichedToolReceipt]:
        store = self._store(investigation_id)
        existing = store.all()
        if existing:
            return existing
        # Recovery path: reload from DB when in-memory store is empty
        try:
            from database.ops_persistence import get_ops_store
            rows = get_ops_store().load_receipts_for_investigation(investigation_id)
            for row in rows:
                r = EnrichedToolReceipt(
                    receipt_id=row["receipt_id"],
                    investigation_id=row["investigation_id"],
                    phase=row.get("phase", ""),
                    worker=row.get("worker", ""),
                    action=row.get("action", ""),
                    intent_summary=row.get("intent_summary", ""),
                    called_at_ms=row.get("called_at_ms", 0.0),
                    latency_ms=row.get("latency_ms", 0.0),
                    status=row.get("status", "success"),
                    result_count=row.get("result_count", 0),
                    noise_ratio=row.get("noise_ratio", 0.0),
                    error_msg=row.get("error_msg", ""),
                    confidence_before=row.get("confidence_before", 0.0),
                    confidence_after=row.get("confidence_after", 0.0),
                )
                store.add(r)
        except Exception:
            pass
        return store.all()

    def get_receipt(self, investigation_id: str, receipt_id: str) -> EnrichedToolReceipt | None:
        return self._store(investigation_id).get(receipt_id)

    def get_evidence_atlas(self, investigation_id: str) -> dict:
        """Return bipartite graph: evidence nodes ↔ hypothesis nodes."""
        receipts = self.get_receipts(investigation_id)
        nodes: list[dict] = []
        edges: list[dict] = []
        hyp_seen: set[str] = set()
        ev_seen: set[str] = set()

        for r in receipts:
            ev_id = f"ev:{r.receipt_id}"
            if ev_id not in ev_seen:
                ev_seen.add(ev_id)
                nodes.append({
                    "id": ev_id,
                    "type": "evidence",
                    "label": f"{r.worker}.{r.action}",
                    "signal_count": r.signal_count,
                    "status": r.status,
                    "latency_ms": r.latency_ms,
                })
            for delta in r.hypothesis_deltas:
                hyp_id = f"hyp:{delta.name}"
                if hyp_id not in hyp_seen:
                    hyp_seen.add(hyp_id)
                    nodes.append({
                        "id": hyp_id,
                        "type": "hypothesis",
                        "label": delta.name,
                        "final_score": delta.score_after,
                    })
                edges.append({
                    "source": ev_id,
                    "target": hyp_id,
                    "weight": abs(delta.delta) / 100.0,
                    "delta": delta.delta,
                })

        return {"nodes": nodes, "edges": edges, "total_receipts": len(receipts)}

    def evict(self, investigation_id: str) -> None:
        """Release memory for a completed investigation."""
        with self._lock:
            self._stores.pop(investigation_id, None)


# Process singleton
_emitter: ToolTransparencyEmitter | None = None
_emitter_lock = threading.Lock()


def get_emitter() -> ToolTransparencyEmitter:
    global _emitter
    if _emitter is None:
        with _emitter_lock:
            if _emitter is None:
                _emitter = ToolTransparencyEmitter()
    return _emitter
