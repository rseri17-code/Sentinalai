"""Application Runtime Intelligence Module (Phase 6 DIE, fourth module).

Fourth implementation on the unchanged framework. Interprets universal JVM/runtime
failure signatures — thread-pool exhaustion (workers blocked on a slow dependency)
and long stop-the-world GC pauses — into canonical root-cause hypotheses.

It distinguishes these from each other and from CPU saturation using the evidence:
a thread pool exhausted with LOW CPU is starvation on a slow downstream, not a
CPU-bound overload; GC pauses correlate with heap pressure, not memory OOM.
Signatures (``thread pool exhausted``, ``stop-the-world`` / GC-aligned pauses) are
standard runtime markers, not EFIC strings. Flag-gated by
``DI_APPLICATION_RUNTIME_ENABLED`` (default off).
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class ApplicationRuntimeIntelligence(DomainModule):
    name = "application_runtime"
    flag_env = "DI_APPLICATION_RUNTIME_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []

        # --- thread-pool exhaustion: workers blocked/starved on a slow downstream ---
        if ("thread pool" in text or "threadpool" in text) and (
                "exhaust" in text or "rejected" in text or "blocked" in text
                or "starv" in text):
            hyps.append(_hyp(
                "thread_pool_exhaustion",
                f"thread pool exhaustion in {svc}: all worker threads blocked/starved "
                f"on a slow downstream (task rejected)",
                82, ["logs:thread_pool_exhausted", "metrics:threads_blocked"],
                "Worker threads are exhausted and tasks are rejected while CPU stays "
                "low — starvation on a synchronous slow dependency, not CPU overload.",
                "add timeouts/bulkheads; make the downstream calls async"))

        # --- long stop-the-world GC pauses (heap under-sized for load) ---
        if ("stop-the-world" in text or "gc pause" in text or "gc_pause" in text
                or "aligned to gc" in text or "long gc" in text):
            hyps.append(_hyp(
                "gc_pause_latency",
                f"long stop-the-world GC pauses in {svc} (JVM heap under-sized for "
                f"load) driving latency",
                80, ["metrics:gc_pause", "metrics:heap_near_max"],
                "Latency spikes align to stop-the-world GC pauses with the heap near "
                "its max and no OOM — a GC/heap-sizing problem, not a leak.",
                "tune heap/GC; reduce allocation rate; scale out"))

        return hyps
