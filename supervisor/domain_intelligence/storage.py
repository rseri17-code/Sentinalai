"""Storage Intelligence Module (Phase 6 DIE). Volume/disk full (ENOSPC).
Flag-gated by DI_STORAGE_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class StorageIntelligence(DomainModule):
    name = "storage"
    flag_env = "DI_STORAGE_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        if ("no space left" in text or "enospc" in text or "disk full" in text
                or "volume_pct=100" in text or "volume full" in text):
            hyps.append(_hyp(
                "storage_volume_full",
                f"storage volume full for {svc}: no space left on device (the PVC/"
                f"volume reached 100%, e.g. retention not pruned)",
                82, ["logs:enospc", "metrics:volume_pct_100"],
                "Writes fail with ENOSPC and the volume is at 100% — the disk/PVC "
                "filled (unpruned retention or a runaway writer).",
                "expand the volume + prune retention; alert on volume utilization"))
        return hyps
