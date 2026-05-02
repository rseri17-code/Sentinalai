"""SLO Engine — tracks error budgets, burn rates, and breach forecasts.

An SLO (Service Level Objective) defines the acceptable reliability target for
a service. The error budget is how much unreliability is permitted in a window.

This engine:
  - Stores SLO definitions per service (target, window, metric type)
  - Computes current error budget consumption from telemetry snapshots
  - Calculates burn rate (how fast the budget is being consumed)
  - Forecasts time-to-breach given current burn rate
  - Emits SLO burn alerts when velocity is dangerous

Key concepts:
  error_budget_remaining  = (1 - target) × window — consumed
  burn_rate               = current_error_rate / (1 - slo_target)
  hours_to_breach         = budget_remaining_hours / burn_rate  (if burn_rate > 1)

  burn_rate > 1    → consuming budget faster than it replenishes → will breach
  burn_rate = 1    → exactly on track
  burn_rate < 1    → better than target, budget accumulating

Persistence: slo_definitions table (PostgreSQL) + in-memory cache.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sentinalai.slo_engine")

SLO_ENABLED = os.environ.get("SLO_ENABLED", "true").lower() in ("1", "true", "yes")

# Default SLO window in days
DEFAULT_SLO_WINDOW_DAYS = int(os.environ.get("SLO_WINDOW_DAYS", "30"))


@dataclass
class SLODefinition:
    """Definition of a service SLO."""
    service: str
    metric: str          # error_rate | latency_p95_ms | availability
    target: float        # 0.999 = 99.9% availability, 0.001 = 0.1% max error rate
    window_days: int     # rolling window in days (typically 30)
    threshold_value: float = 0.0   # for latency: max acceptable ms

    @property
    def error_budget_fraction(self) -> float:
        """The fraction of time/requests that can be in violation."""
        return 1.0 - self.target

    @property
    def window_hours(self) -> float:
        return self.window_days * 24.0

    def budget_hours_total(self) -> float:
        return self.window_hours * self.error_budget_fraction


@dataclass
class SLOStatus:
    """Current SLO health for one service."""
    service: str
    metric: str
    slo_target: float

    # Budget
    budget_total_hours: float        # total error budget in this window
    budget_consumed_hours: float     # how much has been used
    budget_remaining_hours: float    # how much is left
    budget_remaining_pct: float      # 0–100

    # Burn rate
    current_value: float             # current metric value
    burn_rate: float                 # multiples of budget consumption rate
    hours_to_breach: float           # at current burn rate; inf if burn_rate ≤ 1

    # Status
    status: str                      # OK | BURNING | CRITICAL | BREACHED
    observations: int                # data points used in computation

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "metric": self.metric,
            "slo_target": self.slo_target,
            "budget_total_hours": round(self.budget_total_hours, 2),
            "budget_consumed_hours": round(self.budget_consumed_hours, 2),
            "budget_remaining_hours": round(self.budget_remaining_hours, 2),
            "budget_remaining_pct": round(self.budget_remaining_pct, 1),
            "current_value": round(self.current_value, 6),
            "burn_rate": round(self.burn_rate, 3),
            "hours_to_breach": round(self.hours_to_breach, 1) if self.hours_to_breach != float("inf") else None,
            "status": self.status,
            "observations": self.observations,
        }


class SLOEngine:
    """Computes error budget and burn rate for all registered SLOs."""

    # Default SLO definitions — overridden by DB entries if available
    _DEFAULTS: list[dict] = [
        {"service": "api-gateway",      "metric": "error_rate",    "target": 0.999,  "window_days": 30},
        {"service": "payment-service",  "metric": "error_rate",    "target": 0.9995, "window_days": 30},
        {"service": "auth-service",     "metric": "error_rate",    "target": 0.999,  "window_days": 30},
        {"service": "payment-service",  "metric": "latency_p95_ms","target": 0.95,   "window_days": 30, "threshold_value": 500},
    ]

    def __init__(self) -> None:
        self._slos: dict[str, list[SLODefinition]] = {}   # service → [SLODefinition]
        self._load_definitions()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_all(
        self, aggregator: Any
    ) -> list[SLOStatus]:
        """Compute SLO status for all registered services.

        Args:
            aggregator: TelemetryAggregator instance for metric retrieval

        Returns list of SLOStatus for every (service, metric) pair.
        """
        if not SLO_ENABLED:
            return []

        results: list[SLOStatus] = []
        for service, definitions in self._slos.items():
            for defn in definitions:
                status = self._compute_one(defn, aggregator)
                if status:
                    results.append(status)
        return results

    def compute_for_service(
        self, service: str, aggregator: Any
    ) -> list[SLOStatus]:
        """Compute SLO status for a specific service."""
        results = []
        for defn in self._slos.get(service, []):
            status = self._compute_one(defn, aggregator)
            if status:
                results.append(status)
        return results

    def register_slo(
        self,
        service: str,
        metric: str,
        target: float,
        window_days: int = DEFAULT_SLO_WINDOW_DAYS,
        threshold_value: float = 0.0,
    ) -> None:
        """Register or update an SLO definition."""
        defn = SLODefinition(
            service=service, metric=metric, target=target,
            window_days=window_days, threshold_value=threshold_value,
        )
        self._slos.setdefault(service, [])
        # Replace existing for same metric
        self._slos[service] = [
            d for d in self._slos[service] if d.metric != metric
        ]
        self._slos[service].append(defn)
        self._persist_definition(defn)

    def get_breach_risk_services(
        self, statuses: list[SLOStatus]
    ) -> list[SLOStatus]:
        """Return SLOs that are burning budget dangerously or already breached."""
        return [s for s in statuses if s.status in ("BURNING", "CRITICAL", "BREACHED")]

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def _compute_one(
        self, defn: SLODefinition, aggregator: Any
    ) -> SLOStatus | None:
        """Compute SLO status for one (service, metric) pair."""
        if not aggregator.is_baseline_ready(defn.service):
            return None   # cold start — insufficient data

        # Fetch recent metric values over the SLO window
        window_minutes = defn.window_days * 24 * 60
        # For performance, use last 24h for burn rate; use full window for budget consumption
        recent_24h = aggregator.get_recent(defn.service, minutes=60, metric=defn.metric)
        recent_full = aggregator.get_recent(defn.service, minutes=min(window_minutes, 10080), metric=defn.metric)

        if not recent_24h:
            return None

        # Current value = average of last 10 observations (smoothed)
        current_value = sum(v for _, v in recent_24h[-10:]) / len(recent_24h[-10:])

        # Compute budget consumed
        budget_consumed_hours = self._compute_consumed(recent_full, defn)
        budget_total = defn.budget_hours_total()
        budget_remaining = max(0.0, budget_total - budget_consumed_hours)
        budget_remaining_pct = (budget_remaining / budget_total * 100) if budget_total > 0 else 0.0

        # Burn rate: how fast are we consuming the budget relative to the replenishment rate?
        # burn_rate = current_error_fraction / (1 - target)
        if defn.metric == "latency_p95_ms" and defn.threshold_value > 0:
            # For latency SLOs: fraction of requests exceeding threshold
            violation_fraction = max(0.0, (current_value - defn.threshold_value) / defn.threshold_value)
        else:
            violation_fraction = float(current_value)

        budget_fraction = defn.error_budget_fraction
        burn_rate = violation_fraction / budget_fraction if budget_fraction > 0 else 0.0

        # Time to breach at current burn rate
        if burn_rate > 1.0 and budget_remaining > 0:
            # Budget replenishes at 1 unit per window_hours
            # Net consumption rate = (burn_rate - 1) budget_hours per real_hour
            hours_to_breach = budget_remaining / (burn_rate - 1.0) if burn_rate > 1 else float("inf")
        else:
            hours_to_breach = float("inf")

        status = self._classify_status(
            burn_rate, budget_remaining_pct, hours_to_breach
        )

        return SLOStatus(
            service=defn.service,
            metric=defn.metric,
            slo_target=defn.target,
            budget_total_hours=budget_total,
            budget_consumed_hours=budget_consumed_hours,
            budget_remaining_hours=budget_remaining,
            budget_remaining_pct=budget_remaining_pct,
            current_value=current_value,
            burn_rate=burn_rate,
            hours_to_breach=hours_to_breach,
            status=status,
            observations=len(recent_full),
        )

    def _compute_consumed(
        self, series: list[tuple[float, float]], defn: SLODefinition
    ) -> float:
        """Estimate budget hours consumed from a metric time series.

        For each observation interval where the metric exceeds the SLO threshold,
        count that interval duration as consumed budget.

        Returns consumed budget in hours.
        """
        if len(series) < 2:
            return 0.0

        consumed_seconds = 0.0
        for i in range(1, len(series)):
            epoch_prev, val_prev = series[i - 1]
            epoch_curr, val_curr = series[i]
            interval = epoch_curr - epoch_prev   # seconds
            avg_val = (val_prev + val_curr) / 2.0

            if defn.metric == "latency_p95_ms" and defn.threshold_value > 0:
                violating = avg_val > defn.threshold_value
            else:
                violating = avg_val > (1.0 - defn.target)

            if violating:
                consumed_seconds += interval

        return consumed_seconds / 3600.0   # convert to hours

    @staticmethod
    def _classify_status(
        burn_rate: float,
        remaining_pct: float,
        hours_to_breach: float,
    ) -> str:
        if remaining_pct <= 0:
            return "BREACHED"
        if burn_rate > 14.4:            # 1h breach window (6× fast burn)
            return "CRITICAL"
        if burn_rate > 6.0 or hours_to_breach < 6:
            return "BURNING"
        if burn_rate > 1.0:
            return "WATCHING"
        return "OK"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_definitions(self) -> None:
        """Load SLO definitions from DB, fall back to defaults."""
        loaded = self._load_from_db()
        if not loaded:
            for d in self._DEFAULTS:
                defn = SLODefinition(**d)
                self._slos.setdefault(defn.service, []).append(defn)
        logger.debug(
            "SLO engine loaded: %d services, %d SLOs",
            len(self._slos),
            sum(len(v) for v in self._slos.values()),
        )

    def _load_from_db(self) -> bool:
        try:
            from database.persistence import get_engine
            from sqlalchemy import text
            engine = get_engine()
            if engine is None:
                return False
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT service, metric, target, window_days, threshold_value "
                    "FROM slo_definitions"
                ))
                for row in rows.fetchall():
                    defn = SLODefinition(
                        service=row[0], metric=row[1], target=float(row[2]),
                        window_days=int(row[3]), threshold_value=float(row[4] or 0),
                    )
                    self._slos.setdefault(defn.service, []).append(defn)
            return bool(self._slos)
        except Exception as exc:
            logger.debug("SLO DB load skipped: %s", exc)
            return False

    def _persist_definition(self, defn: SLODefinition) -> None:
        try:
            from database.persistence import get_engine
            from sqlalchemy import text
            engine = get_engine()
            if engine is None:
                return
            with engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO slo_definitions
                        (service, metric, target, window_days, threshold_value)
                    VALUES
                        (:service, :metric, :target, :window_days, :threshold_value)
                    ON CONFLICT (service, metric) DO UPDATE
                        SET target=EXCLUDED.target,
                            window_days=EXCLUDED.window_days,
                            threshold_value=EXCLUDED.threshold_value,
                            updated_at=NOW()
                """), {
                    "service": defn.service, "metric": defn.metric,
                    "target": defn.target, "window_days": defn.window_days,
                    "threshold_value": defn.threshold_value,
                })
                conn.commit()
        except Exception as exc:
            logger.debug("SLO persist failed: %s", exc)
