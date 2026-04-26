"""Tests for supervisor/slack_bot.py — Slack Block Kit formatter."""

from __future__ import annotations

import pytest

from supervisor.slack_bot import (
    SlackFormatter,
    SlackMessage,
    _confidence_bar,
    _severity_label,
    verify_slack_signature,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestConfidenceBar:
    def test_zero(self):
        bar = _confidence_bar(0)
        assert "0%" in bar

    def test_hundred(self):
        bar = _confidence_bar(100)
        assert "100%" in bar
        assert "░" not in bar  # all filled

    def test_fifty(self):
        bar = _confidence_bar(50)
        assert "50%" in bar

    def test_clamps_above_100(self):
        bar = _confidence_bar(150)
        assert "100%" in bar

    def test_clamps_below_zero(self):
        bar = _confidence_bar(-10)
        assert "0%" in bar

    def test_contains_brackets(self):
        bar = _confidence_bar(80)
        assert "[" in bar and "]" in bar


class TestSeverityLabel:
    def test_integer_critical(self):
        assert _severity_label(4) == "CRITICAL"

    def test_integer_high(self):
        assert _severity_label(3) == "HIGH"

    def test_string_low(self):
        assert _severity_label("low") == "LOW"

    def test_string_upper(self):
        assert _severity_label("HIGH") == "HIGH"

    def test_unknown_string(self):
        result = _severity_label("emergency")
        assert result == "EMERGENCY"


# ---------------------------------------------------------------------------
# SlackFormatter — investigation_started
# ---------------------------------------------------------------------------

class TestInvestigationStarted:
    def _make(self, **kw) -> SlackMessage:
        defaults = dict(
            incident_id="INC-12378",
            investigation_id="inv-abc123",
            service="payment-service",
            severity="critical",
            description="Stripe API degradation",
        )
        return SlackFormatter.investigation_started(**{**defaults, **kw})

    def test_returns_slack_message(self):
        msg = self._make()
        assert isinstance(msg, SlackMessage)

    def test_text_contains_incident_id(self):
        msg = self._make()
        assert "INC-12378" in msg.text

    def test_text_contains_service(self):
        msg = self._make()
        assert "payment-service" in msg.text

    def test_blocks_non_empty(self):
        msg = self._make()
        assert len(msg.blocks) >= 2

    def test_header_block_present(self):
        msg = self._make()
        assert any(b["type"] == "header" for b in msg.blocks)

    def test_severity_critical_in_title(self):
        msg = self._make(severity="critical")
        header = next(b for b in msg.blocks if b["type"] == "header")
        assert "CRITICAL" in header["text"]["text"]

    def test_description_in_blocks(self):
        msg = self._make(description="Stripe timeout")
        texts = [str(b) for b in msg.blocks]
        assert any("Stripe timeout" in t for t in texts)

    def test_no_unicode_emoji(self):
        msg = self._make()
        for char in msg.text:
            assert ord(char) < 0x1F300 or ord(char) > 0x1FAFF, f"Emoji found: {char}"

    def test_default_channel_set(self):
        msg = self._make()
        assert msg.channel != ""

    def test_custom_channel(self):
        msg = self._make(channel="#sre-p1")
        assert msg.channel == "#sre-p1"


# ---------------------------------------------------------------------------
# SlackFormatter — rca_complete
# ---------------------------------------------------------------------------

class TestRcaComplete:
    def _make(self, **kw) -> SlackMessage:
        defaults = dict(
            incident_id="INC-12378",
            investigation_id="inv-abc123",
            service="payment-service",
            severity="high",
            root_cause="Stripe API degradation — external vendor timeout",
            confidence=87,
            blast_radius_level="low",
            blast_radius_safe=True,
        )
        return SlackFormatter.rca_complete(**{**defaults, **kw})

    def test_returns_slack_message(self):
        assert isinstance(self._make(), SlackMessage)

    def test_text_contains_confidence(self):
        msg = self._make(confidence=87)
        assert "87" in msg.text

    def test_text_contains_root_cause(self):
        msg = self._make()
        assert "Stripe" in msg.text or "timeout" in msg.text

    def test_blocks_have_actions(self):
        msg = self._make()
        assert any(b["type"] == "actions" for b in msg.blocks)

    def test_approve_button_present(self):
        msg = self._make()
        actions = next(b for b in msg.blocks if b["type"] == "actions")
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "approve_fix" in action_ids

    def test_escalate_button_present(self):
        msg = self._make()
        actions = next(b for b in msg.blocks if b["type"] == "actions")
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "escalate" in action_ids

    def test_override_button_present(self):
        msg = self._make()
        actions = next(b for b in msg.blocks if b["type"] == "actions")
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "override_rca" in action_ids

    def test_safe_label_when_safe(self):
        msg = self._make(blast_radius_safe=True)
        texts = " ".join(str(b) for b in msg.blocks)
        assert "safe" in texts.lower() or "Safe" in texts

    def test_approval_label_when_not_safe(self):
        msg = self._make(blast_radius_safe=False)
        texts = " ".join(str(b) for b in msg.blocks)
        assert "approval" in texts.lower() or "Approval" in texts

    def test_immediate_actions_in_blocks(self):
        msg = self._make(immediate_actions=["Enable circuit breaker", "Monitor error rate"])
        texts = " ".join(str(b) for b in msg.blocks)
        assert "circuit breaker" in texts.lower()

    def test_proposed_fix_in_blocks(self):
        msg = self._make(proposed_fix="kubectl rollback payment-service")
        texts = " ".join(str(b) for b in msg.blocks)
        assert "kubectl" in texts

    def test_historical_match_in_blocks(self):
        msg = self._make(similar_incident_id="INC-12301", similar_incident_age="3 weeks")
        texts = " ".join(str(b) for b in msg.blocks)
        assert "INC-12301" in texts

    def test_thread_ts_passed_through(self):
        msg = self._make(thread_ts="1234567890.123456")
        assert msg.thread_ts == "1234567890.123456"


# ---------------------------------------------------------------------------
# SlackFormatter — proactive_alert
# ---------------------------------------------------------------------------

class TestProactiveAlert:
    def _make(self, **kw) -> SlackMessage:
        defaults = dict(
            service="payment-service",
            metric_name="memory_utilisation",
            current_value=87.3,
            threshold=90.0,
            urgency="WARNING",
            trend_direction="rising",
        )
        return SlackFormatter.proactive_alert(**{**defaults, **kw})

    def test_returns_slack_message(self):
        assert isinstance(self._make(), SlackMessage)

    def test_text_contains_service(self):
        msg = self._make()
        assert "payment-service" in msg.text

    def test_urgency_in_text(self):
        msg = self._make(urgency="IMMINENT")
        assert "IMMINENT" in msg.text

    def test_metric_in_blocks(self):
        msg = self._make(metric_name="cpu_utilisation")
        texts = " ".join(str(b) for b in msg.blocks)
        assert "cpu_utilisation" in texts

    def test_investigate_button_present(self):
        msg = self._make()
        actions = next((b for b in msg.blocks if b["type"] == "actions"), None)
        assert actions is not None
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "investigate_now" in action_ids

    def test_acknowledge_button_present(self):
        msg = self._make()
        actions = next(b for b in msg.blocks if b["type"] == "actions")
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "acknowledge_alert" in action_ids

    def test_minutes_to_breach_in_blocks(self):
        msg = self._make(minutes_to_breach=12.5)
        texts = " ".join(str(b) for b in msg.blocks)
        assert "12" in texts

    def test_recommended_action_in_blocks(self):
        msg = self._make(recommended_action="Capture heap dump immediately")
        texts = " ".join(str(b) for b in msg.blocks)
        assert "heap dump" in texts


# ---------------------------------------------------------------------------
# SlackFormatter — shift_handoff
# ---------------------------------------------------------------------------

class TestShiftHandoff:
    def _brief(self, **kw) -> dict:
        defaults = {
            "outgoing_engineer": "alice",
            "incoming_engineer": "bob",
            "summary": "3 fragile services, 1 active investigation.",
            "fragile_services": [
                {
                    "service": "payment-service",
                    "risk_level": "critical",
                    "incident_count_7d": 5,
                    "reason": "5 incidents in 7 days",
                }
            ],
            "active_investigations": [{"incident_id": "INC-12378", "status": "investigating"}],
            "upcoming_risk": [],
            "conditional_guidance": [],
        }
        defaults.update(kw)
        return defaults

    def test_returns_slack_message(self):
        assert isinstance(SlackFormatter.shift_handoff(self._brief()), SlackMessage)

    def test_outgoing_engineer_in_text(self):
        msg = SlackFormatter.shift_handoff(self._brief())
        assert "alice" in msg.text

    def test_incoming_engineer_in_text(self):
        msg = SlackFormatter.shift_handoff(self._brief())
        assert "bob" in msg.text

    def test_fragile_service_in_blocks(self):
        msg = SlackFormatter.shift_handoff(self._brief())
        texts = " ".join(str(b) for b in msg.blocks)
        assert "payment-service" in texts

    def test_active_investigation_in_blocks(self):
        msg = SlackFormatter.shift_handoff(self._brief())
        texts = " ".join(str(b) for b in msg.blocks)
        assert "INC-12378" in texts


# ---------------------------------------------------------------------------
# SlackFormatter — postmortem_ready
# ---------------------------------------------------------------------------

class TestPostmortemReady:
    def test_incident_id_in_text(self):
        msg = SlackFormatter.postmortem_ready("INC-12378", "payment-service", 45, 3)
        assert "INC-12378" in msg.text

    def test_action_count_in_blocks(self):
        msg = SlackFormatter.postmortem_ready("INC-12378", "payment-service", 45, 7)
        texts = " ".join(str(b) for b in msg.blocks)
        assert "7" in texts

    def test_review_button_present(self):
        msg = SlackFormatter.postmortem_ready("INC-12378", "payment-service", 45, 3)
        actions = next(b for b in msg.blocks if b["type"] == "actions")
        action_ids = [e.get("action_id") for e in actions["elements"]]
        assert "review_postmortem" in action_ids

    def test_duration_in_blocks(self):
        msg = SlackFormatter.postmortem_ready("INC-12378", "payment-service", 45, 3)
        texts = " ".join(str(b) for b in msg.blocks)
        assert "45" in texts


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

class TestVerifySlackSignature:
    def test_empty_secret_returns_true(self):
        assert verify_slack_signature(b"body", "1234567890", "v0=abc", "") is True

    def test_old_timestamp_rejected(self):
        old_ts = "1000000000"  # Very old timestamp
        result = verify_slack_signature(b"body", old_ts, "v0=abc", "secret")
        assert result is False

    def test_invalid_timestamp_rejected(self):
        result = verify_slack_signature(b"body", "not-a-number", "v0=abc", "secret")
        assert result is False
