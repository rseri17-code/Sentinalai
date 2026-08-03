"""Kubernetes Intelligence Module (Phase 6 DIE, second module).

Second implementation on the SAME framework as Database — no framework change, no
duplicated abstraction. It interprets universal Kubernetes failure signatures
(CrashLoopBackOff, ImagePullBackOff, node DiskPressure/eviction, readiness-probe
failure on a slow dependency) into canonical root-cause hypotheses.

Signatures are standard Kubernetes event/condition markers, not EFIC strings, so
the module generalizes to any real cluster incident of these modes. Flag-gated by
``DI_KUBERNETES_ENABLED`` (default off). (OOMKilled is owned by the II-1 evidence-
driven reclassification path, so it is intentionally not duplicated here.)
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class KubernetesIntelligence(DomainModule):
    name = "kubernetes"
    flag_env = "DI_KUBERNETES_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []

        # --- CrashLoopBackOff: pod crashing on startup (often bad config) ---
        if "crashloopbackoff" in text:
            config_driven = any(t in text for t in (
                "config key", "missing config", "invalid config", "configmap",
                "fatal"))
            cause = ("invalid/missing configuration crashing" if config_driven
                     else "a startup failure crashing")
            hyps.append(_hyp(
                "kubernetes_crashloopbackoff",
                f"CrashLoopBackOff: {cause} {svc} on startup",
                82, ["events:crashloopbackoff", "logs:startup_error"],
                "Pods enter CrashLoopBackOff, restarting on startup — a container "
                "that exits non-zero before becoming ready.",
                "revert the bad config/image; add startup config validation"))

        # --- ImagePullBackOff: image tag / manifest missing in the registry ---
        if "imagepullbackoff" in text or ("manifest" in text and "not found" in text):
            hyps.append(_hyp(
                "kubernetes_imagepullbackoff",
                f"ImagePullBackOff: {svc} image tag/manifest missing in the registry",
                82, ["events:imagepullbackoff"],
                "Pods cannot pull their image — the tag/manifest is absent in the "
                "registry (e.g. the image build/publish step was skipped).",
                "publish the missing image tag or roll back the deployment"))

        # --- node pressure eviction: ephemeral-storage / disk exhaustion ---
        if ("evicted" in text or "diskpressure" in text or "disk pressure" in text
                or "ephemeral" in text):
            hyps.append(_hyp(
                "kubernetes_node_pressure_eviction",
                f"node pressure eviction: pods evicted from ephemeral disk "
                f"exhaustion on {svc}'s node",
                81, ["events:evicted", "metrics:node_ephemeral"],
                "The node reports DiskPressure and evicts pods — ephemeral storage "
                "filled (often a noisy log-writing neighbor).",
                "set ephemeral-storage limits; cap the noisy neighbor"))

        # --- readiness-probe failure gating traffic on a slow dependency ---
        if ("readiness" in text or "notready" in text or "not ready" in text) and (
                "probe" in text or "readiness" in text):
            hyps.append(_hyp(
                "kubernetes_readiness_probe_failure",
                f"readiness probe failure on a slow downstream — pods marked "
                f"NotReady for {svc}",
                80, ["events:readiness_failing"],
                "The readiness probe checks a slow downstream, so healthy pods are "
                "marked NotReady and pulled from the load balancer.",
                "scope readiness to local health; fix the downstream latency"))

        return hyps
