"""Certificate Intelligence Module (Phase 6 DIE). Expired TLS certificate causing
handshake failures. Flag-gated by DI_CERTIFICATES_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class CertificateIntelligence(DomainModule):
    name = "certificates"
    flag_env = "DI_CERTIFICATES_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        if ("certificate expired" in text or "cert expired" in text
                or ("certificate" in text and "expired" in text)
                or ("tls handshake" in text and "expired" in text)):
            hyps.append(_hyp(
                "certificate_expiry",
                f"expired TLS certificate on {svc} causing handshake failures",
                84, ["logs:tls_handshake_failure", "certificates:expired"],
                "Clients fail the TLS handshake with a certificate-expired error — "
                "the cert lapsed (renewal automation likely failed silently).",
                "rotate the certificate; fix + alert on the renewal automation"))
        return hyps
