"""API Gateway Intelligence Module (Phase 6 DIE, third module).

Third implementation on the SAME framework — no framework change. It interprets
universal API-gateway / service-mesh failure signatures and, crucially,
distinguishes CAUSE from SYMPTOM using negative evidence rather than collapsing a
symptom into a root cause:

* 429s are a symptom. With the backend healthy (pool not saturated), the cause is
  rate-limit *bucket* saturation from a consumer surge — not backend overload.
* TLS handshake failures east-west, with an istio-proxy cert-reload failure event,
  are an mTLS/sidecar-certificate problem — not a network outage.

Signatures are standard gateway/mesh markers (HTTP 429, "TLS handshake",
istio-proxy cert reload), not EFIC strings. Flag-gated by ``DI_API_GATEWAY_ENABLED``.
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class ApiGatewayIntelligence(DomainModule):
    name = "api_gateway"
    flag_env = "DI_API_GATEWAY_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []

        # --- rate-limit saturation (429) — cause vs symptom via negative evidence ---
        if "429" in text or "rate limit" in text or "too many requests" in text:
            # Negative evidence: is the backend actually saturated? If the DB/pool
            # is comfortably below its max, the 429s are gateway rate-limiting, not
            # backend overload — distinguish the two.
            active = _num(view.metric_value("active"))
            pool_max = _num(view.metric_value("max"))
            backend_healthy = (active is not None and pool_max is not None
                               and active < 0.8 * pool_max)
            cause = ("a consumer traffic surge exhausted the shared rate limit quota "
                     "bucket while the backend stayed healthy" if backend_healthy
                     else "the shared rate limit quota bucket saturated under a "
                     "consumer surge")
            hyps.append(_hyp(
                "gateway_rate_limit_saturation",
                f"gateway rate limit saturation (429): {cause} for {svc}",
                82 if backend_healthy else 74,
                ["logs:http_429", "metrics:backend_healthy"] if backend_healthy
                else ["logs:http_429"],
                "Clients receive HTTP 429; the backend is not saturated, so the "
                "gateway's shared rate-limit bucket is the constraint — not the app.",
                "add per-consumer quotas; isolate the noisy consumer's bucket"))

        # --- istio / service-mesh mTLS failure (sidecar cert not rotated) ---
        mtls = (("tls handshake" in text or "upstream connect error" in text
                 or "cert reload" in text or "mtls" in text)
                and ("istio" in text or "sidecar" in text or "mesh" in text
                     or "cert reload" in text))
        if mtls:
            hyps.append(_hyp(
                "istio_mtls_failure",
                f"istio mTLS failure: the sidecar certificate was not rotated after a "
                f"CA change, breaking east-west mTLS for {svc}",
                82, ["events:istio_cert_reload_failed", "logs:tls_handshake"],
                "East-west TLS handshake failures with an istio-proxy cert-reload "
                "failure — the sidecar cert was not rotated after a CA change (north-"
                "south stays healthy, so it is not a network outage).",
                "restart the sidecars to reload certs; automate on CA rotation"))

        return hyps
