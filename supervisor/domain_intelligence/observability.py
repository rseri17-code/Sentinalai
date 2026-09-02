"""Observability Intelligence Module (Phase 6 DIE). Detects an observability blind
spot: real errors in the logs while the metrics pipeline is stalled/lagging, so
dashboards under-report the failure. Flag-gated by DI_OBSERVABILITY_ENABLED (off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class ObservabilityIntelligence(DomainModule):
    name = "observability"
    flag_env = "DI_OBSERVABILITY_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        # A metrics-pipeline stall (ingestion stalled / ingest lag / 0% error while
        # real errors are logged) means dashboards under-report a genuine failure.
        metrics_stalled = ("ingestion stalled" in text or "ingest_lag" in text
                           or "ingest lag" in text or "metrics gap" in text
                           or "0% (metrics" in text or "no metrics" in text)
        real_errors = any(e.get("message") for e in view.logs)
        if metrics_stalled and real_errors:
            hyps.append(_hyp(
                "observability_blind_spot",
                f"observability blind spot: a metrics-pipeline gap (ingestion "
                f"stalled) hid a real {svc} error rate — the failure is genuine but "
                f"under-reported by missing telemetry",
                82, ["metrics:ingest_lag", "logs:real_errors_vs_zero_dashboard"],
                "Logs and tickets show real failures while the metrics dashboard "
                "reads ~0% — the APM ingestion is stalled, so the green dashboard is "
                "a blind spot, not evidence of health.",
                "restore the metrics pipeline; alert on ingestion lag / metric gaps"))
        return hyps
