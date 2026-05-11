"""Prediction Store — persists predictions and tracks outcomes for calibration.

Every prediction the Pattern Intelligence Layer makes is stored here with
its outcome tracked. This closes the self-improvement loop:

  prediction made → outcome tracked → calibration updated → future predictions improve

Outcome tracking:
  - If a matching incident fires within the breach_window → TRUE POSITIVE
  - If no incident fires within 2× predicted_breach_hours → FALSE POSITIVE
  - FALSE POSITIVEs are fed into the ConfidenceCalibrator as negative signals
  - TRUE POSITIVEs are fed as positive signals

Alert fatigue mitigation:
  - Deduplication: same (service, pattern_type, metric) suppressed for cooldown_minutes
  - Severity gate: only WATCH+ published to Intelligence Feed
  - False positive feedback: engineers can mark predictions as noise via API

Cold-start handling:
  - Predictions flagged with baseline_ready=False are stored but not published
  - UI shows "Pattern intelligence warming up for {service}"
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.prediction_store")

PREDICTION_COOLDOWN_MINUTES = int(os.environ.get("PREDICTION_COOLDOWN_MINUTES", "30"))
PREDICTION_RETENTION_DAYS   = int(os.environ.get("PREDICTION_RETENTION_DAYS", "30"))

_store_lock = threading.Lock()

# In-memory dedup index: (service, pattern_type, metric) → last_published_epoch
_dedup_index: dict[tuple, float] = {}


@dataclass
class Prediction:
    """A stored prediction with outcome tracking."""
    prediction_id: str
    service: str
    pattern_type: str
    severity: str              # WATCH | LIKELY | IMMINENT
    metric: str
    confidence: float
    current_value: float
    explanation: str
    predicted_breach_hours: float | None
    related_service: str
    evidence: dict

    # Outcome
    outcome: str = "pending"   # pending | true_positive | false_positive | expired
    outcome_incident_id: str = ""
    outcome_resolved_at: str = ""

    # Meta
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_at_epoch: float = field(default_factory=time.time)
    published: bool = True     # False = suppressed (dedup or cold-start)
    baseline_ready: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def is_expired(self) -> bool:
        """Prediction is expired if breach window has passed without incident."""
        if self.predicted_breach_hours is None:
            # Use 2× COOLDOWN as expiry for predictions with no breach estimate
            return time.time() - self.created_at_epoch > PREDICTION_COOLDOWN_MINUTES * 120
        expiry = self.created_at_epoch + self.predicted_breach_hours * 3600 * 2
        return time.time() > expiry


class PredictionStore:
    """Manages prediction lifecycle: store → deduplicate → track → calibrate."""

    def __init__(self) -> None:
        self._predictions: dict[str, Prediction] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(
        self,
        detection: Any,
        baseline_ready: bool = True,
    ) -> Prediction | None:
        """Store a detection as a prediction.

        Returns the stored Prediction, or None if suppressed by dedup.
        """
        dedup_key = (detection.service, detection.pattern_type, detection.metric)
        cooldown_sec = PREDICTION_COOLDOWN_MINUTES * 60

        with _store_lock:
            last_published = _dedup_index.get(dedup_key, 0)
            is_duplicate = (time.time() - last_published) < cooldown_sec

            published = (not is_duplicate) and baseline_ready
            if published:
                _dedup_index[dedup_key] = time.time()

        pred = Prediction(
            prediction_id=str(uuid.uuid4()),
            service=detection.service,
            pattern_type=detection.pattern_type,
            severity=detection.severity,
            metric=detection.metric,
            confidence=detection.confidence,
            current_value=detection.current_value,
            explanation=detection.explanation,
            predicted_breach_hours=detection.predicted_breach_hours,
            related_service=detection.related_service,
            evidence=detection.evidence,
            published=published,
            baseline_ready=baseline_ready,
        )

        self._predictions[pred.prediction_id] = pred
        self._persist_prediction(pred)
        if published:
            self._persist_pattern_event(pred, "pending")

        if not published:
            logger.debug(
                "Prediction suppressed (dedup=%s baseline=%s): %s/%s",
                is_duplicate, not baseline_ready, detection.service, detection.pattern_type,
            )
        else:
            logger.info(
                "Prediction published: %s %s %s confidence=%.2f",
                detection.severity, detection.service, detection.pattern_type, detection.confidence,
            )

        return pred if published else None

    def record_outcome(
        self,
        service: str,
        incident_id: str,
        pattern_type: str = "",
    ) -> int:
        """Mark open predictions for a service as true positives.

        Called when an incident fires for the service, closing the feedback loop.
        Returns number of predictions resolved.
        """
        resolved = 0
        now = datetime.now(timezone.utc).isoformat()

        for pred in self._predictions.values():
            if pred.service != service:
                continue
            if pred.outcome != "pending":
                continue
            if pattern_type and pred.pattern_type != pattern_type:
                continue

            pred.outcome = "true_positive"
            pred.outcome_incident_id = incident_id
            pred.outcome_resolved_at = now
            self._update_outcome_in_db(pred)
            self._feed_calibration(pred)
            self._persist_pattern_event(pred, "true_positive")
            resolved += 1

        logger.info(
            "Prediction outcomes recorded: service=%s incident=%s resolved=%d",
            service, incident_id, resolved,
        )
        return resolved

    def mark_false_positive(self, prediction_id: str, reason: str = "") -> bool:
        """Engineer-provided false positive feedback."""
        pred = self._predictions.get(prediction_id)
        if not pred:
            return False
        pred.outcome = "false_positive"
        pred.outcome_resolved_at = datetime.now(timezone.utc).isoformat()
        self._update_outcome_in_db(pred)
        self._feed_calibration(pred)
        self._persist_pattern_event(pred, "false_positive")
        logger.info("Prediction %s marked as false positive: %s", prediction_id, reason)
        return True

    def expire_old_predictions(self) -> int:
        """Mark expired pending predictions as false positives."""
        expired = 0
        for pred in list(self._predictions.values()):
            if pred.outcome == "pending" and pred.is_expired():
                pred.outcome = "false_positive"
                pred.outcome_resolved_at = datetime.now(timezone.utc).isoformat()
                self._update_outcome_in_db(pred)
                self._feed_calibration(pred)
                self._persist_pattern_event(pred, "expired")
                expired += 1
        if expired:
            logger.info("Expired %d predictions → false_positive", expired)
        return expired

    def get_active_predictions(self, min_severity: str = "WATCH") -> list[Prediction]:
        """Return published, pending predictions above minimum severity."""
        order = {"WATCH": 0, "LIKELY": 1, "IMMINENT": 2}
        min_order = order.get(min_severity, 0)
        return [
            p for p in self._predictions.values()
            if p.published
            and p.outcome == "pending"
            and order.get(p.severity, 0) >= min_order
        ]

    def get_accuracy_report(self) -> dict[str, Any]:
        """Return prediction accuracy statistics by pattern type."""
        by_type: dict[str, dict[str, int]] = {}
        for pred in self._predictions.values():
            if pred.outcome == "pending":
                continue
            pt = pred.pattern_type
            by_type.setdefault(pt, {"true_positive": 0, "false_positive": 0, "expired": 0})
            if pred.outcome in by_type[pt]:
                by_type[pt][pred.outcome] += 1

        report: dict[str, Any] = {}
        for pt, counts in by_type.items():
            total = counts["true_positive"] + counts["false_positive"] + counts.get("expired", 0)
            precision = counts["true_positive"] / total if total else 0.0
            report[pt] = {**counts, "total": total, "precision": round(precision, 3)}

        return {"by_pattern_type": report, "total_predictions": len(self._predictions)}

    # ------------------------------------------------------------------
    # Calibration feedback
    # ------------------------------------------------------------------

    def _feed_calibration(self, pred: Prediction) -> None:
        """Send prediction outcome to ConfidenceCalibrator."""
        try:
            from supervisor.confidence_calibrator import get_calibrator, _calibrator_lock
            actual_correct = pred.outcome == "true_positive"
            data = [{"predicted_confidence": int(pred.confidence * 100), "actual_correct": actual_correct}]
            with _calibrator_lock:
                cal = get_calibrator()
                cal.update_with_decay(data, decay_factor=0.98)
                cal.save()
            logger.debug(
                "Calibration updated from prediction outcome: %s correct=%s",
                pred.prediction_id[:8], actual_correct,
            )
        except Exception as exc:
            logger.debug("Calibration feed failed: %s", exc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_pattern_event(self, pred: Prediction, outcome: str) -> None:
        try:
            from database.ops_persistence import get_ops_store
            get_ops_store().persist_pattern_event(
                pattern_type=pred.pattern_type,
                prediction_id=pred.prediction_id,
                service=pred.service,
                outcome=outcome,
                confidence=pred.confidence,
            )
        except Exception as exc:
            logger.debug("persist_pattern_event failed: %s", exc)

    def _persist_prediction(self, pred: Prediction) -> None:
        try:
            from database.persistence import get_engine
            from sqlalchemy import text
            engine = get_engine()
            if engine is None:
                return
            with engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO pattern_predictions
                        (prediction_id, service, pattern_type, severity, metric,
                         confidence, current_value, explanation, predicted_breach_hours,
                         related_service, evidence, published, outcome)
                    VALUES
                        (:pid, :service, :ptype, :severity, :metric,
                         :confidence, :current_value, :explanation, :breach_hours,
                         :related, :evidence::jsonb, :published, :outcome)
                    ON CONFLICT (prediction_id) DO NOTHING
                """), {
                    "pid": pred.prediction_id,
                    "service": pred.service,
                    "ptype": pred.pattern_type,
                    "severity": pred.severity,
                    "metric": pred.metric,
                    "confidence": pred.confidence,
                    "current_value": pred.current_value,
                    "explanation": pred.explanation,
                    "breach_hours": pred.predicted_breach_hours,
                    "related": pred.related_service,
                    "evidence": json.dumps(pred.evidence),
                    "published": pred.published,
                    "outcome": pred.outcome,
                })
                conn.commit()
        except Exception as exc:
            logger.debug("Persist prediction failed: %s", exc)

    def _update_outcome_in_db(self, pred: Prediction) -> None:
        try:
            from database.persistence import get_engine
            from sqlalchemy import text
            engine = get_engine()
            if engine is None:
                return
            with engine.connect() as conn:
                conn.execute(text("""
                    UPDATE pattern_predictions
                    SET outcome = :outcome,
                        outcome_incident_id = :incident_id,
                        outcome_resolved_at = NOW()
                    WHERE prediction_id = :pid
                """), {
                    "outcome": pred.outcome,
                    "incident_id": pred.outcome_incident_id,
                    "pid": pred.prediction_id,
                })
                conn.commit()
        except Exception as exc:
            logger.debug("Update outcome failed: %s", exc)
