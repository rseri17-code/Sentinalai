"""Cloud Intelligence Module (Phase 6 DIE). AWS availability-zone impairment:
errors concentrated in a single AZ while others are normal. Flag-gated by
DI_CLOUD_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class CloudIntelligence(DomainModule):
    name = "cloud"
    flag_env = "DI_CLOUD_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        # Errors isolated to one availability zone (others normal) => AZ impairment,
        # not an app bug or a deploy (which would hit all AZs).
        az_concentrated = (("errors_by_az" in text or "availability zone" in text
                            or "us-east-1" in text or " az " in text or "az=" in text)
                           and ("high" in text or "degraded" in text
                                or "impair" in text))
        if az_concentrated:
            hyps.append(_hyp(
                "aws_az_impairment",
                f"AWS availability zone (AZ) impairment: regional degradation "
                f"isolated to one AZ, degrading {svc} nodes in that zone",
                82, ["metrics:errors_by_az", "metrics:az_degraded"],
                "Errors are concentrated in one availability zone while the others "
                "are normal — an AZ-scoped infrastructure impairment, not an app bug "
                "or a fleet-wide deploy (which would affect all zones).",
                "drain/fail over the impaired AZ; enable cross-AZ failover"))
        return hyps
