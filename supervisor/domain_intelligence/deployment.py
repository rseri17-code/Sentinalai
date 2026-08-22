"""Deployment Intelligence Module (Phase 6 DIE, fifth module).

Fifth implementation on the unchanged framework. It performs change-correlation
reasoning — the CAUSE of an error spike right after a release is the release, not
the exception it surfaced — and distinguishes a code regression from configuration
drift:

* an application exception correlated in time with a recent release/deploy change
  => a code regression introduced by that release;
* a subset of nodes reporting invalid/inconsistent config (version mismatch) with
  no bad release => configuration drift, not a code regression.

Signatures (application exceptions + a release/deploy change record; "invalid
config" across "some nodes") are standard deployment markers, not EFIC strings.
Flag-gated by ``DI_DEPLOYMENT_ENABLED`` (default off).
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp

_RELEASE_WORDS = ("release", "deploy", "rollout", "rolled out", "new version")
_ERROR_WORDS = ("exception", "nullpointer", "null pointer", "npe", "error rate",
                "5xx", "500 ", "stack trace", "traceback", "failure rate")


def _version(text: str) -> str:
    """Extract a version token like 'v8.4' from evidence text (deterministic)."""
    for tok in text.replace(",", " ").split():
        t = tok.strip("().;:")
        if len(t) >= 2 and t[0] == "v" and t[1].isdigit():
            return t
    return ""


class DeploymentIntelligence(DomainModule):
    name = "deployment"
    flag_env = "DI_DEPLOYMENT_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        changes_text = " ".join(
            str(c.get("short_description", c.get("description", "")))
            for c in view.changes).lower()
        hyps: list[dict] = []

        # --- code regression introduced by a recent release/deploy ---
        has_error = any(w in text for w in _ERROR_WORDS)
        release_change = any(w in changes_text for w in _RELEASE_WORDS)
        if has_error and release_change:
            ver = _version(text) or _version(changes_text)
            ver_str = f" ({ver})" if ver else ""
            hyps.append(_hyp(
                "release_regression",
                f"code regression introduced by the recent release/deployment{ver_str} "
                f"to {svc}",
                82, ["logs:exception", "changes:recent_release"],
                "An application exception appeared right after a release change — the "
                "regression was introduced by the deployment, not a pre-existing bug.",
                f"roll back {svc} to the previous release; add pre-prod coverage"))

        # --- configuration drift: inconsistent config across a subset of nodes ---
        if ("config" in text and ("node" in text or "inconsistent" in text
                                  or "drift" in text)) or "config drift" in text:
            hyps.append(_hyp(
                "config_drift",
                f"configuration drift: a subset of {svc} nodes came up with "
                f"inconsistent (drifted) config",
                80, ["logs:invalid_config", "cmdb:config_version_mismatch"],
                "Only some nodes report invalid config while others are healthy — the "
                "new nodes launched with a drifted (stale) configuration.",
                "reconcile the launch template; enforce a config-parity check"))

        return hyps
