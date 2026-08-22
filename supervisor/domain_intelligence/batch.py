"""Batch Intelligence Module (Phase 6 DIE). Scheduled batch job terminated on a
missing upstream file dependency. Flag-gated by DI_BATCH_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class BatchIntelligence(DomainModule):
    name = "batch"
    flag_env = "DI_BATCH_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        if ("upstream file" in text and ("not found" in text or "missing" in text)) \
                or "missing upstream file" in text or "file not found" in text:
            hyps.append(_hyp(
                "batch_upstream_dependency_failure",
                f"scheduled batch job for {svc} terminated on a missing upstream file "
                f"dependency (upstream extract slipped its SLA)",
                82, ["logs:upstream_file_not_found", "autosys:job_terminated"],
                "The batch job terminated because a required upstream file was absent "
                "— the upstream producer missed its SLA; no dependency gate caught it.",
                "add an upstream-file dependency gate + SLA alert to the job"))
        return hyps
