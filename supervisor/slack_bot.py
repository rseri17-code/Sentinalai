"""Slack Bot — Block Kit message formatter and HTTP poster for SentinalAI.

Sends rich, actionable Slack messages during incident investigations:
  - Investigation started (live progress indicator)
  - RCA complete (root cause + evidence + action buttons)
  - Proactive pre-incident alerts (from sentinel loop)
  - Shift handoff brief
  - Postmortem ready for review

All formatting uses Slack Block Kit. HTTP calls use httpx (already a project dep).
Configure via env vars: SLACK_BOT_TOKEN, SLACK_DEFAULT_CHANNEL, SLACK_SIGNING_SECRET.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("sentinalai.slack_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL", "#sre-incidents")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
AGUI_BASE_URL = os.getenv("AGUI_BASE_URL", "http://localhost:8081")

_SEVERITY_LABEL = {
    1: "LOW",
    2: "MEDIUM",
    3: "HIGH",
    4: "CRITICAL",
    5: "CRITICAL",
    "low":      "LOW",
    "medium":   "MEDIUM",
    "high":     "HIGH",
    "critical": "CRITICAL",
}

_SEVERITY_PREFIX = {
    "LOW":      "[LOW]",
    "MEDIUM":   "[MEDIUM]",
    "HIGH":     "[HIGH]",
    "CRITICAL": "[CRITICAL]",
}

_URGENCY_PREFIX = {
    "WATCH":    "[WATCH]",
    "WARNING":  "[WARNING]",
    "IMMINENT": "[IMMINENT]",
    "BREACHED": "[BREACHED]",
}

_RISK_PREFIX = {
    "low":      "[LOW RISK]",
    "medium":   "[MEDIUM RISK]",
    "high":     "[HIGH RISK]",
    "critical": "[CRITICAL RISK]",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _confidence_bar(confidence: int | float, width: int = 10) -> str:
    """Return an ASCII confidence bar like [████████░░] 84%."""
    pct = max(0, min(100, int(confidence)))
    filled = round(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


def _severity_label(severity: Any) -> str:
    return _SEVERITY_LABEL.get(severity, str(severity).upper())


def _deep_link(investigation_id: str) -> str:
    return f"{AGUI_BASE_URL}/investigations/{investigation_id}"


def _mrkdwn(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text, "emoji": False}


def _section(text: str, fields: list[dict] | None = None) -> dict:
    block: dict[str, Any] = {"type": "section", "text": _mrkdwn(text)}
    if fields:
        block["fields"] = fields
    return block


def _header(text: str) -> dict:
    return {"type": "header", "text": _plain(text[:150])}


def _divider() -> dict:
    return {"type": "divider"}


def _context(*texts: str) -> dict:
    return {"type": "context", "elements": [_mrkdwn(t) for t in texts]}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    btn: dict[str, Any] = {
        "type": "button",
        "text": _plain(text),
        "action_id": action_id,
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


def _actions(*buttons: dict) -> dict:
    return {"type": "actions", "elements": list(buttons)}


# ---------------------------------------------------------------------------
# Message formatters
# ---------------------------------------------------------------------------

@dataclass
class SlackMessage:
    text: str                            # Fallback / notification text
    blocks: list[dict] = field(default_factory=list)
    channel: str = ""
    thread_ts: str = ""                  # Set to reply in a thread


class SlackFormatter:
    """Stateless Block Kit formatters. Each method returns a SlackMessage."""

    # ------------------------------------------------------------------ #
    # Investigation Started
    # ------------------------------------------------------------------ #
    @staticmethod
    def investigation_started(
        incident_id: str,
        investigation_id: str,
        service: str,
        severity: Any,
        description: str = "",
        channel: str = "",
    ) -> SlackMessage:
        sev = _severity_label(severity)
        prefix = _SEVERITY_PREFIX.get(sev, f"[{sev}]")
        title = f"{prefix} {service} — Investigation Started"

        blocks = [
            _header(title),
            _section(
                f"*Incident:* `{incident_id}`\n"
                f"*Service:* `{service}`\n"
                f"*Severity:* {sev}\n"
                + (f"*Summary:* {description[:200]}" if description else ""),
            ),
            _divider(),
            _section(
                "_SentinalAI is investigating. Collecting evidence from logs, "
                "metrics, APM traces, and change history..._"
            ),
            _context(
                f"Investigation: `{investigation_id}`",
                f"<{_deep_link(investigation_id)}|View live progress>",
            ),
        ]

        return SlackMessage(
            text=f"{prefix} {service} — SentinalAI investigating {incident_id}",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
        )

    # ------------------------------------------------------------------ #
    # Investigation Progress (update / edit the original message)
    # ------------------------------------------------------------------ #
    @staticmethod
    def investigation_progress(
        incident_id: str,
        investigation_id: str,
        service: str,
        phase: str,
        tool_calls_made: int,
        budget: int,
        hypothesis_count: int = 0,
        channel: str = "",
        thread_ts: str = "",
    ) -> SlackMessage:
        phase_label = phase.replace("_", " ").title()
        pct = round(tool_calls_made / max(budget, 1) * 100)
        bar = _confidence_bar(pct, width=8)

        blocks = [
            _header(f"Investigating {service}... ({phase_label})"),
            _section(
                f"*Phase:* {phase_label}\n"
                f"*Progress:* {bar}\n"
                f"*Tool calls:* {tool_calls_made}/{budget}\n"
                f"*Hypotheses generated:* {hypothesis_count}"
            ),
            _context(
                f"`{incident_id}` | `{investigation_id}`",
                f"<{_deep_link(investigation_id)}|Watch live>",
            ),
        ]

        return SlackMessage(
            text=f"Investigating {service} — {phase_label} ({tool_calls_made}/{budget} calls)",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
            thread_ts=thread_ts,
        )

    # ------------------------------------------------------------------ #
    # RCA Complete — the most important message in the system
    # ------------------------------------------------------------------ #
    @staticmethod
    def rca_complete(
        incident_id: str,
        investigation_id: str,
        service: str,
        severity: Any,
        root_cause: str,
        confidence: int | float,
        reasoning: str = "",
        blast_radius_level: str = "unknown",
        blast_radius_safe: bool = False,
        similar_incident_id: str = "",
        similar_incident_age: str = "",
        proposed_fix: str = "",
        immediate_actions: list[str] | None = None,
        elapsed_seconds: int = 0,
        channel: str = "",
        thread_ts: str = "",
    ) -> SlackMessage:
        sev = _severity_label(severity)
        prefix = _SEVERITY_PREFIX.get(sev, f"[{sev}]")
        conf_bar = _confidence_bar(confidence)
        br_prefix = _RISK_PREFIX.get(blast_radius_level.lower(), "[UNKNOWN RISK]")
        safe_label = "Safe to auto-apply" if blast_radius_safe else "Requires approval"

        actions_text = ""
        if immediate_actions:
            actions_text = "\n".join(f"• {a}" for a in immediate_actions[:3])

        history_text = ""
        if similar_incident_id:
            history_text = (
                f"*Historical match:* `{similar_incident_id}`"
                + (f" ({similar_incident_age} ago)" if similar_incident_age else "")
            )

        elapsed_label = f"{elapsed_seconds}s" if elapsed_seconds else "—"

        blocks = [
            _header(f"{prefix} Root Cause Found — {int(confidence)}% Confidence"),
            _section(
                f"*Root Cause:*\n{root_cause}",
                fields=[
                    _mrkdwn(f"*Service:*\n`{service}`"),
                    _mrkdwn(f"*Severity:*\n{sev}"),
                    _mrkdwn(f"*Confidence:*\n{conf_bar}"),
                    _mrkdwn(f"*Blast Radius:*\n{br_prefix}"),
                    _mrkdwn(f"*Fix Safety:*\n{safe_label}"),
                    _mrkdwn(f"*Investigation Time:*\n{elapsed_label}"),
                ],
            ),
        ]

        if reasoning:
            blocks.append(_section(f"*Why:*\n_{reasoning[:300]}_"))

        if history_text:
            blocks.append(_section(history_text))

        if actions_text:
            blocks.append(_section(f"*Immediate Actions:*\n{actions_text}"))

        if proposed_fix:
            blocks.append(_section(f"*Proposed Fix:*\n`{proposed_fix[:200]}`"))

        blocks.append(_divider())
        blocks.append(
            _actions(
                _button("Approve Fix", "approve_fix", investigation_id, style="primary"),
                _button("View Evidence", "view_evidence", investigation_id),
                _button("Override", "override_rca", investigation_id),
                _button("Escalate", "escalate", investigation_id, style="danger"),
            )
        )
        blocks.append(
            _context(
                f"`{incident_id}` | `{investigation_id}`",
                f"<{_deep_link(investigation_id)}|Full report + evidence trail>",
            )
        )

        return SlackMessage(
            text=f"{prefix} {service} — Root cause found ({int(confidence)}% confidence): {root_cause[:100]}",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
            thread_ts=thread_ts,
        )

    # ------------------------------------------------------------------ #
    # Proactive Pre-Incident Alert (from sentinel loop)
    # ------------------------------------------------------------------ #
    @staticmethod
    def proactive_alert(
        service: str,
        metric_name: str,
        current_value: float,
        threshold: float,
        urgency: str,
        trend_direction: str = "rising",
        minutes_to_breach: float | None = None,
        recommended_action: str = "",
        channel: str = "",
    ) -> SlackMessage:
        prefix = _URGENCY_PREFIX.get(urgency.upper(), f"[{urgency.upper()}]")
        breach_label = (
            f"{int(minutes_to_breach)}min to breach" if minutes_to_breach else "Threshold already breached"
        )
        pct_of_threshold = round(current_value / threshold * 100) if threshold else 0
        trend_bar = _confidence_bar(pct_of_threshold)

        blocks = [
            _header(f"{prefix} Pre-Incident Signal — {service}"),
            _section(
                f"*Metric:* `{metric_name}`\n"
                f"*Current:* {current_value:.2f}  (threshold: {threshold:.2f})\n"
                f"*Trend:* {trend_direction.upper()}  {trend_bar}\n"
                f"*Time to breach:* {breach_label}"
            ),
        ]

        if recommended_action:
            blocks.append(_section(f"*Recommended action:*\n_{recommended_action}_"))

        blocks.append(
            _actions(
                _button("Investigate Now", "investigate_now", f"{service}|{metric_name}",
                        style="primary"),
                _button("Acknowledge", "acknowledge_alert", f"{service}|{metric_name}"),
            )
        )
        blocks.append(
            _context(
                f"SentinalAI Sentinel Loop | Urgency: {urgency}",
                f"Signal detected before PagerDuty alert fires",
            )
        )

        return SlackMessage(
            text=f"{prefix} {service} — {metric_name} is {trend_direction} ({breach_label})",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
        )

    # ------------------------------------------------------------------ #
    # Shift Handoff Brief
    # ------------------------------------------------------------------ #
    @staticmethod
    def shift_handoff(
        brief_dict: dict,
        channel: str = "",
    ) -> SlackMessage:
        outgoing = brief_dict.get("outgoing_engineer", "outgoing-sre")
        incoming = brief_dict.get("incoming_engineer", "incoming-sre")
        fragile = brief_dict.get("fragile_services", [])
        active = brief_dict.get("active_investigations", [])
        upcoming = brief_dict.get("upcoming_risk", [])
        summary = brief_dict.get("summary", "")

        blocks = [
            _header(f"Shift Handoff: {outgoing} -> {incoming}"),
            _section(summary if summary else "No significant events this shift."),
            _divider(),
        ]

        if fragile:
            lines = []
            for svc in fragile[:5]:
                risk = svc.get("risk_level", "elevated").upper()
                count = svc.get("incident_count_7d", 0)
                name = svc.get("service", "?")
                lines.append(f"• [{risk}] `{name}` — {count} incidents in 7d")
            blocks.append(_section("*Fragile Services (watch closely)*\n" + "\n".join(lines)))

        if active:
            iids = [i.get("incident_id", i.get("id", "?")) for i in active[:3]]
            blocks.append(_section(
                f"*Active Investigations ({len(active)})*\n"
                + "\n".join(f"• `{iid}`" for iid in iids)
            ))

        if upcoming:
            lines = []
            for c in upcoming[:3]:
                risk = c.get("risk_level", "medium").upper()
                svc = c.get("service", "?")
                ct = c.get("change_type", "?")
                lines.append(f"• [{risk}] `{svc}` — {ct}")
            blocks.append(_section("*Upcoming Changes*\n" + "\n".join(lines)))

        blocks.append(_context("Generated by SentinalAI Shift Intelligence"))

        return SlackMessage(
            text=f"Shift Handoff: {outgoing} -> {incoming}",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
        )

    # ------------------------------------------------------------------ #
    # Postmortem Ready
    # ------------------------------------------------------------------ #
    @staticmethod
    def postmortem_ready(
        incident_id: str,
        service: str,
        duration_minutes: int,
        action_item_count: int,
        report_url: str = "",
        channel: str = "",
    ) -> SlackMessage:
        blocks = [
            _header(f"Postmortem Draft Ready — {incident_id}"),
            _section(
                f"*Service:* `{service}`\n"
                f"*Duration:* {duration_minutes} minutes\n"
                f"*Action items:* {action_item_count} generated\n"
                f"*Status:* DRAFT — requires review"
            ),
            _divider(),
            _actions(
                _button("Review & Approve", "review_postmortem", incident_id, style="primary"),
                _button("View Draft", "view_postmortem", incident_id),
                _button("Publish to Confluence", "publish_postmortem", incident_id),
            ),
        ]
        if report_url:
            blocks.append(_context(f"<{report_url}|View full postmortem>"))
        blocks.append(_context("Generated by SentinalAI Postmortem Studio | Blameless by design"))

        return SlackMessage(
            text=f"Postmortem draft ready for {incident_id} — {action_item_count} action items",
            blocks=blocks,
            channel=channel or SLACK_DEFAULT_CHANNEL,
        )


# ---------------------------------------------------------------------------
# HTTP poster
# ---------------------------------------------------------------------------

class SlackBot:
    """Posts SlackMessage objects to the Slack API via chat.postMessage / chat.update."""

    def __init__(self, token: str = "", default_channel: str = "") -> None:
        self._token = token or SLACK_BOT_TOKEN
        self._channel = default_channel or SLACK_DEFAULT_CHANNEL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def post(self, msg: SlackMessage, channel: str = "") -> dict:
        """Post a new message. Returns the Slack API response dict."""
        if not self._token:
            logger.warning("SLACK_BOT_TOKEN not set — message not sent: %s", msg.text[:80])
            return {"ok": False, "error": "no_token"}
        try:
            import httpx  # optional dep — already in pyproject.toml
            payload: dict[str, Any] = {
                "channel": channel or msg.channel or self._channel,
                "text": msg.text,
                "blocks": msg.blocks,
            }
            if msg.thread_ts:
                payload["thread_ts"] = msg.thread_ts
            resp = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error("Slack API error: %s", data.get("error"))
            return data
        except Exception as exc:
            logger.exception("Failed to post Slack message: %s", exc)
            return {"ok": False, "error": str(exc)}

    def update(self, msg: SlackMessage, ts: str, channel: str = "") -> dict:
        """Update an existing message (e.g., refresh progress)."""
        if not self._token:
            return {"ok": False, "error": "no_token"}
        try:
            import httpx
            payload = {
                "channel": channel or msg.channel or self._channel,
                "ts": ts,
                "text": msg.text,
                "blocks": msg.blocks,
            }
            resp = httpx.post(
                "https://slack.com/api/chat.update",
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
            return resp.json()
        except Exception as exc:
            logger.exception("Failed to update Slack message: %s", exc)
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Signature verification (for incoming webhook validation)
# ---------------------------------------------------------------------------

def verify_slack_signature(
    body: bytes,
    timestamp: str,
    signature: str,
    signing_secret: str = "",
) -> bool:
    """Return True if the Slack request signature is valid."""
    secret = signing_secret or SLACK_SIGNING_SECRET
    if not secret:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping signature verification")
        return True

    # Reject requests older than 5 minutes (replay attack prevention)
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except ValueError:
        return False

    base = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    expected = "v0=" + hmac.new(
        secret.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_bot: SlackBot | None = None


def get_bot() -> SlackBot:
    global _bot
    if _bot is None:
        _bot = SlackBot()
    return _bot
