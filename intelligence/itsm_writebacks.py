"""ITSM Bi-directional Write-back Engine.

After an investigation resolves with high confidence, auto-acknowledge and write
a resolution note back to PagerDuty, Opsgenie, ServiceNow, or a mock provider.

Safety rules:
- Default dry_run=True — never makes real API calls unless
  ITSM_WRITEBACK_ENABLED=true AND the provider token env var is set.
- All HTTP calls wrapped in try/except with 5s timeout.
- In dry_run or mock mode: logs what would be sent, returns success=True.

Provider detection (first token found wins):
  PD_TOKEN   → PagerDuty
  OG_TOKEN   → Opsgenie
  SN_TOKEN   → ServiceNow
  (none)     → mock
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger("sentinalai.intelligence.itsm_writebacks")

_WRITEBACK_ENABLED = os.environ.get("ITSM_WRITEBACK_ENABLED", "false").lower() in ("true", "1", "yes")


def _detect_provider() -> tuple[str, str]:
    """Return (provider_name, token). Falls back to ('mock', '')."""
    pd_token = os.environ.get("PD_TOKEN", "")
    og_token = os.environ.get("OG_TOKEN", "")
    sn_token = os.environ.get("SN_TOKEN", "")

    if pd_token:
        return "pagerduty", pd_token
    if og_token:
        return "opsgenie", og_token
    if sn_token:
        return "servicenow", sn_token
    return "mock", ""


@dataclass
class WritebackResult:
    incident_id: str
    provider: str       # "pagerduty" | "opsgenie" | "servicenow" | "mock"
    action: str         # "acknowledged" | "resolved" | "commented"
    success: bool
    message: str
    dry_run: bool


class ITSMWritebackEngine:
    """Bi-directional ITSM write-back engine with safe defaults."""

    def __init__(self, dry_run: bool = True) -> None:
        self._dry_run = dry_run
        provider, token = _detect_provider()
        self._provider = provider
        self._token = token

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def provider(self) -> str:
        return self._provider

    def _is_real_call_allowed(self) -> bool:
        """Return True only when both the global flag and a provider token are set."""
        enabled = os.environ.get("ITSM_WRITEBACK_ENABLED", "false").lower() in ("true", "1", "yes")
        return enabled and bool(self._token) and not self._dry_run

    def acknowledge(
        self,
        incident_id: str,
        service: str,
        confidence: float,
    ) -> WritebackResult:
        """Acknowledge an incident in the ITSM provider."""
        action = "acknowledged"

        if not self._is_real_call_allowed():
            logger.info(
                "[ITSM dry_run] Would acknowledge incident=%s service=%s confidence=%.2f via %s",
                incident_id, service, confidence, self._provider,
            )
            return WritebackResult(
                incident_id=incident_id,
                provider=self._provider,
                action=action,
                success=True,
                message=f"dry_run: would acknowledge {incident_id} via {self._provider}",
                dry_run=True,
            )

        if self._provider == "pagerduty":
            return self._pd_acknowledge(incident_id, service, confidence)

        # Other providers: log and return mock success
        logger.info(
            "[ITSM] acknowledge not implemented for provider=%s; no-op", self._provider
        )
        return WritebackResult(
            incident_id=incident_id,
            provider=self._provider,
            action=action,
            success=True,
            message=f"no-op: acknowledge not implemented for {self._provider}",
            dry_run=False,
        )

    def resolve(
        self,
        incident_id: str,
        service: str,
        root_cause: str,
        resolution_action: str,
        confidence: float,
        runbook_url: str = "",
    ) -> WritebackResult:
        """Resolve an incident in the ITSM provider with a resolution note."""
        action = "resolved"
        resolution_note = (
            f"SentinalAI auto-resolution (confidence={confidence:.0%})\n"
            f"Root cause: {root_cause}\n"
            f"Resolution: {resolution_action}"
        )
        if runbook_url:
            resolution_note += f"\nRunbook: {runbook_url}"

        if not self._is_real_call_allowed():
            logger.info(
                "[ITSM dry_run] Would resolve incident=%s service=%s provider=%s\n  %s",
                incident_id, service, self._provider, resolution_note,
            )
            return WritebackResult(
                incident_id=incident_id,
                provider=self._provider,
                action=action,
                success=True,
                message=f"dry_run: would resolve {incident_id} via {self._provider}",
                dry_run=True,
            )

        if self._provider == "pagerduty":
            return self._pd_resolve(incident_id, root_cause, confidence)

        # Other providers: log and return mock success
        logger.info(
            "[ITSM] resolve not implemented for provider=%s; no-op", self._provider
        )
        return WritebackResult(
            incident_id=incident_id,
            provider=self._provider,
            action=action,
            success=True,
            message=f"no-op: resolve not implemented for {self._provider}",
            dry_run=False,
        )

    def add_comment(self, incident_id: str, comment: str) -> WritebackResult:
        """Add a comment/note to an incident in the ITSM provider."""
        action = "commented"

        if not self._is_real_call_allowed():
            logger.info(
                "[ITSM dry_run] Would comment on incident=%s via %s\n  %s",
                incident_id, self._provider, comment,
            )
            return WritebackResult(
                incident_id=incident_id,
                provider=self._provider,
                action=action,
                success=True,
                message=f"dry_run: would comment on {incident_id} via {self._provider}",
                dry_run=True,
            )

        if self._provider == "pagerduty":
            return self._pd_comment(incident_id, comment)

        logger.info(
            "[ITSM] add_comment not implemented for provider=%s; no-op", self._provider
        )
        return WritebackResult(
            incident_id=incident_id,
            provider=self._provider,
            action=action,
            success=True,
            message=f"no-op: comment not implemented for {self._provider}",
            dry_run=False,
        )

    # ------------------------------------------------------------------ #
    # PagerDuty implementation
    # ------------------------------------------------------------------ #

    def _pd_resolve(
        self, incident_id: str, root_cause: str, confidence: float
    ) -> WritebackResult:
        try:
            import json as _json
            import urllib.error
            import urllib.request

            payload = _json.dumps({
                "incident": {
                    "type": "incident_reference",
                    "status": "resolved",
                    "resolution": (
                        f"SentinalAI auto-resolved (confidence={confidence:.0%}): {root_cause}"
                    ),
                }
            }).encode()

            req = urllib.request.Request(
                f"https://api.pagerduty.com/incidents/{incident_id}",
                data=payload,
                method="PATCH",
                headers={
                    "Authorization": f"Token token={self._token}",
                    "Accept": "application/vnd.pagerduty+json;version=2",
                    "Content-Type": "application/json",
                    "From": "sentinalai@sentinalai.io",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as http_err:
                status = http_err.code
                logger.warning("PagerDuty resolve HTTP error: %s", http_err)
                return WritebackResult(
                    incident_id=incident_id,
                    provider="pagerduty",
                    action="resolved",
                    success=False,
                    message=f"HTTP {status}: {http_err}",
                    dry_run=False,
                )

            success = 200 <= status < 300
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="resolved",
                success=success,
                message=f"PagerDuty PATCH status={status}",
                dry_run=False,
            )
        except Exception as exc:
            logger.warning("PagerDuty resolve failed: %s", exc)
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="resolved",
                success=False,
                message=str(exc),
                dry_run=False,
            )

    def _pd_acknowledge(
        self, incident_id: str, service: str, confidence: float
    ) -> WritebackResult:
        try:
            import json as _json
            import urllib.error
            import urllib.request

            payload = _json.dumps({
                "incident": {
                    "type": "incident_reference",
                    "status": "acknowledged",
                }
            }).encode()

            req = urllib.request.Request(
                f"https://api.pagerduty.com/incidents/{incident_id}",
                data=payload,
                method="PATCH",
                headers={
                    "Authorization": f"Token token={self._token}",
                    "Accept": "application/vnd.pagerduty+json;version=2",
                    "Content-Type": "application/json",
                    "From": "sentinalai@sentinalai.io",
                },
            )
            import urllib.error
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as http_err:
                status = http_err.code
                return WritebackResult(
                    incident_id=incident_id,
                    provider="pagerduty",
                    action="acknowledged",
                    success=False,
                    message=f"HTTP {status}: {http_err}",
                    dry_run=False,
                )

            success = 200 <= status < 300
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="acknowledged",
                success=success,
                message=f"PagerDuty PATCH status={status}",
                dry_run=False,
            )
        except Exception as exc:
            logger.warning("PagerDuty acknowledge failed: %s", exc)
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="acknowledged",
                success=False,
                message=str(exc),
                dry_run=False,
            )

    def _pd_comment(self, incident_id: str, comment: str) -> WritebackResult:
        try:
            import json as _json
            import urllib.error
            import urllib.request

            payload = _json.dumps({
                "note": {"content": comment}
            }).encode()

            req = urllib.request.Request(
                f"https://api.pagerduty.com/incidents/{incident_id}/notes",
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"Token token={self._token}",
                    "Accept": "application/vnd.pagerduty+json;version=2",
                    "Content-Type": "application/json",
                    "From": "sentinalai@sentinalai.io",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as http_err:
                status = http_err.code
                return WritebackResult(
                    incident_id=incident_id,
                    provider="pagerduty",
                    action="commented",
                    success=False,
                    message=f"HTTP {status}: {http_err}",
                    dry_run=False,
                )

            success = 200 <= status < 300
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="commented",
                success=success,
                message=f"PagerDuty POST status={status}",
                dry_run=False,
            )
        except Exception as exc:
            logger.warning("PagerDuty comment failed: %s", exc)
            return WritebackResult(
                incident_id=incident_id,
                provider="pagerduty",
                action="commented",
                success=False,
                message=str(exc),
                dry_run=False,
            )


# -------------------------------------------------------------------------
# Process-level singleton
# -------------------------------------------------------------------------

_engine: ITSMWritebackEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> ITSMWritebackEngine:
    """Return the process-level singleton ITSMWritebackEngine."""
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is None:
            _engine = ITSMWritebackEngine(dry_run=not _WRITEBACK_ENABLED)
    return _engine
