"""Network Intelligence Module (Phase 6 DIE). Upstream-path packet loss (504 with a
healthy backend) and security-group ingress blocks. Flag-gated by
DI_NETWORK_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class NetworkIntelligence(DomainModule):
    name = "network"
    flag_env = "DI_NETWORK_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        changes = " ".join(str(c.get("short_description", c.get("description", "")))
                           for c in view.changes).lower()
        hyps: list[dict] = []

        # Security-group / firewall change removed ingress -> connectivity blocked.
        if (("security group" in changes or "sg edit" in changes
             or "ingress" in changes) and ("remove" in changes or "block" in changes)):
            hyps.append(_hyp(
                "security_group_ingress_block",
                f"a security group change blocked ingress on a required port for {svc}",
                82, ["changes:sg_edit_remove_ingress", "logs:connection_timeout"],
                "Connections time out to a port right after a security-group change "
                "that removed the ingress rule — connectivity is blocked by policy.",
                "restore the ingress rule; add security-group change review"))

        # 504 / upstream timeouts with a HEALTHY backend => upstream network path
        # packet loss, not backend overload (negative evidence).
        if "504" in text or ("upstream" in text and "timeout" in text):
            active = _num(view.metric_value("active"))
            pool_max = _num(view.metric_value("max"))
            backend_healthy = (active is not None and pool_max is not None
                               and active < 0.5 * pool_max)
            if backend_healthy:
                hyps.append(_hyp(
                    "network_packet_loss",
                    f"network packet loss on the upstream path causing 504 timeouts "
                    f"for {svc} (backend healthy)",
                    80, ["logs:upstream_timeout_504", "metrics:backend_healthy"],
                    "Intermittent 504 upstream timeouts while the backend pool is "
                    "idle — the fault is on the network path, not the application.",
                    "engage network on the edge->core path; fail over the link"))
        return hyps
