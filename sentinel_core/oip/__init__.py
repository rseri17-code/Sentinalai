"""Operational Intelligence Platform (OIP) — user-facing services.

Read-only, produce-only aggregation over completed investigation outputs. These
services compose existing platform capabilities (investigation artifacts,
R1 corpus/replay, R2 evidence/confidence provenance, shadow-pilot observation
records); they add no reasoning, no new intelligence, and never touch runtime.
"""
from sentinel_core.oip.application_health import application_health
from sentinel_core.oip.daily_operations_brief import daily_operations_brief
from sentinel_core.oip.incident_trends import incident_trends
from sentinel_core.oip.operational_health import operational_health
from sentinel_core.oip.service_reliability import service_reliability

__all__ = ["application_health", "daily_operations_brief", "incident_trends",
           "operational_health", "service_reliability"]
