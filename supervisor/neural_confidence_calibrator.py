"""Neural confidence calibrator — learned Platt scaling.

Supplements the 10-bin lookup in ConfidenceCalibrator with a small MLP
that maps (raw_confidence, evidence_features) → P(correct).  The neural
model captures non-linear interactions between confidence and evidence
quality that the binned approach cannot (e.g. "confidence 70 with 4 sources
is more trustworthy than confidence 80 with 1 source").

Architecture:  5 inputs → 16 hidden (leaky ReLU) → 1 output (sigmoid)
Optimizer:     SGD with heavy-ball (classical) momentum  (lr=0.03, momentum=0.9)
Training:      Online — one step per evaluation with ground truth
Persistence:   eval/neural_confidence_calibrator.json

Feature vector (5 dimensions, all in [0, 1]):
  0  raw_conf      raw_confidence / 100
  1  source_frac   min(source_count / 5, 1.0)
  2  volume        evidence_volume from OnlineEvaluator
  3  coherence     evidence_coherence from OnlineEvaluator
  4  specificity   root_cause_specificity from OnlineEvaluator

Target:
  actual_correct ∈ {0, 1} from GroundTruthEvaluator
  (pseudo-label: online_quality_score when GT not available)

  WARNING — pseudo-label feedback loop: when ground truth is unavailable,
  this model trains on the heuristic OnlineEvaluator score.  If the
  heuristic has a systematic bias, the neural model can amplify it rather
  than correct it.  Prefer ground-truth labels whenever possible; limit
  pseudo-label training to cold-start.

Output:  calibrated confidence in [0, 100]

Blending with binned calibrator:
  calibrated = (1 - alpha) * binned + alpha * neural
  alpha = min(1.0, samples / MIN_SAMPLES_FULL) * MAX_BLEND  (max 60%)

  This ensures the rock-solid binned calibrator dominates until the neural
  model has sufficient training data to be reliable.

Implementation note:
  Uses _MLP from supervisor.neural_quality_net rather than duplicating MLP
  code.  The calibrator uses a smaller architecture (5→16→1) and a tighter
  gradient clip (3.0 vs 5.0) to reflect the simpler calibration task.

Usage:
    from supervisor.neural_confidence_calibrator import get_neural_calibrator

    ncal = get_neural_calibrator()
    calibrated = ncal.calibrate_with_context(raw_conf, evidence_context)
    ncal.train_from_result(result, actual_correct)
    ncal.save()
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading

from supervisor.neural_quality_net import _MLP

logger = logging.getLogger("sentinalai.neural_conf_cal")

_MODEL_PATH = os.environ.get(
    "NEURAL_CONF_CAL_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "neural_confidence_calibrator.json"),
)

MIN_SAMPLES_FULL: int = 50    # samples needed for full blend weight
MAX_BLEND: float = 0.60       # max fraction from neural model

_LR: float = 0.03
_MOMENTUM: float = 0.90
_GRAD_CLIP: float = 3.0       # tighter clip than quality net (simpler task)
_LAYER_SIZES: list[int] = [5, 16, 1]


# ---------------------------------------------------------------------------
# NeuralConfidenceCalibrator
# ---------------------------------------------------------------------------

class NeuralConfidenceCalibrator:
    """Learned confidence calibration with evidence-aware context.

    Wraps _MLP (shared with NeuralQualityNet) with calibration-specific
    feature building, blend logic, and persistence.
    """

    def __init__(self, mlp: _MLP | None = None) -> None:
        self._mlp = mlp or _MLP(
            _LAYER_SIZES, lr=_LR, momentum=_MOMENTUM, grad_clip=_GRAD_CLIP, seed=1
        )
        self._lock = threading.Lock()

    @property
    def total_samples(self) -> int:
        return self._mlp.total_samples

    def blend_alpha(self) -> float:
        """Neural blend weight in [0, MAX_BLEND].  Zero until 5 samples."""
        if self._mlp.total_samples < 5:
            return 0.0
        frac = min(1.0, self._mlp.total_samples / MIN_SAMPLES_FULL)
        return frac * MAX_BLEND

    def _build_features(
        self,
        raw_confidence: int,
        source_count: int = 0,
        dims: dict[str, float] | None = None,
    ) -> list[float]:
        d = dims or {}
        return [
            min(1.0, max(0.0, raw_confidence / 100.0)),
            min(1.0, source_count / 5.0),
            float(d.get("volume", 0.0)),
            float(d.get("coherence", 0.0)),
            float(d.get("specificity", 0.0)),
        ]

    def calibrate_with_context(
        self,
        raw_confidence: int,
        evidence_context: dict | None = None,
    ) -> float | None:
        """Return neural-calibrated confidence in [0, 100], or None if model
        is not yet trained enough to contribute (alpha == 0).

        evidence_context can be either:
          - A result dict already annotated with _online_eval (post-analysis), OR
          - The raw evidence dict gathered by the playbook (pre-analysis), in
            which case source_count is computed from the evidence keys directly.
        """
        alpha = self.blend_alpha()
        if alpha == 0.0 or evidence_context is None:
            return None

        oe = evidence_context.get("_online_eval") or {}
        if oe:
            source_count = oe.get("source_count", 0)
            dims = oe.get("dimensions", {})
        else:
            # Raw evidence dict — compute source_count directly from keys
            source_count = self._source_count_from_evidence(evidence_context)
            dims = {}

        features = self._build_features(raw_confidence, source_count, dims)

        with self._lock:
            p_correct = self._mlp.predict(features)

        return p_correct * 100.0

    @staticmethod
    def _source_count_from_evidence(evidence: dict) -> int:
        """Count distinct source categories present in a raw evidence dict."""
        _SOURCE_MARKERS = {
            "logs":    ("search_logs", "get_error_logs", "search_error_logs",
                        "search_timeout_logs", "search_oom_logs"),
            "metrics": ("query_metrics", "query_response_time", "query_error_rate",
                        "query_memory_metrics", "query_cpu_metrics"),
            "signals": ("get_golden_signals", "check_golden_signals", "get_apm_signals"),
            "events":  ("get_k8s_events", "get_events", "get_network_events"),
            "changes": ("get_change_data", "get_recent_deployments", "get_config_changes"),
        }
        found: set[str] = set()
        for ev_key in evidence:
            if ev_key.startswith("_"):
                continue
            for cat, markers in _SOURCE_MARKERS.items():
                if any(m in ev_key for m in markers):
                    found.add(cat)
                    break
        return len(found)

    def train_one(
        self,
        raw_confidence: int,
        actual_correct: float,
        source_count: int = 0,
        dims: dict[str, float] | None = None,
    ) -> float:
        """One SGD step. Returns MSE loss. Thread-safe."""
        features = self._build_features(raw_confidence, source_count, dims)
        with self._lock:
            return self._mlp.train_one(features, actual_correct)

    def train_from_result(self, result: dict, actual_correct: float) -> float:
        """Convenience: extract features from a result dict and train."""
        raw_conf = int(result.get("raw_confidence", result.get("confidence", 0)))
        oe = result.get("_online_eval", {})
        source_count = oe.get("source_count", 0) if oe else 0
        dims = oe.get("dimensions", {}) if oe else {}
        return self.train_one(raw_conf, actual_correct, source_count, dims)

    def save(self, path: str | None = None) -> None:
        path = path or _MODEL_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "model": self._mlp.to_dict()}, f)
        os.replace(tmp, path)
        logger.debug(
            "NeuralConfidenceCalibrator saved: samples=%d", self._mlp.total_samples
        )

    @classmethod
    def load(cls, path: str | None = None) -> "NeuralConfidenceCalibrator":
        path = path or _MODEL_PATH
        try:
            with open(path) as f:
                data = json.load(f)
            mlp = _MLP.from_dict(data["model"])
            # Restore calibrator-specific grad_clip if checkpoint pre-dates this field
            if "grad_clip" not in data["model"]:
                mlp.grad_clip = _GRAD_CLIP
            logger.info("NeuralConfidenceCalibrator loaded: samples=%d", mlp.total_samples)
            return cls(mlp)
        except FileNotFoundError:
            logger.debug("NeuralConfidenceCalibrator not found — fresh model")
            return cls()
        except Exception as exc:
            logger.warning("NeuralConfidenceCalibrator load failed (%s) — fresh model", exc)
            return cls()

    def get_report(self) -> dict:
        return {
            "total_samples": self._mlp.total_samples,
            "blend_alpha": round(self.blend_alpha(), 4),
            "active": self._mlp.total_samples >= 5,
            "layer_sizes": self._mlp.layer_sizes,
            "min_samples_full": MIN_SAMPLES_FULL,
        }

    def get_arch_stats(self) -> dict:
        """Return architecture metadata + per-layer weight norms for UI visualization."""
        mlp = self._mlp
        weight_norms = [
            round(math.sqrt(sum(w * w for row in layer_W for w in row)), 4)
            for layer_W in mlp.W
        ]
        bias_norms = [
            round(math.sqrt(sum(b * b for b in layer_b)), 4)
            for layer_b in mlp.b
        ]
        return {
            "layer_sizes": mlp.layer_sizes,
            "weight_norms": weight_norms,
            "bias_norms": bias_norms,
            "total_samples": mlp.total_samples,
            "blend_alpha": round(self.blend_alpha(), 4),
            "active": mlp.total_samples >= 5,
            "lr": mlp.lr,
            "momentum": mlp.momentum,
            "grad_clip": mlp.grad_clip,
        }


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------

_ncal: NeuralConfidenceCalibrator | None = None
_ncal_lock = threading.RLock()


def get_neural_calibrator() -> NeuralConfidenceCalibrator:
    """Return the process-level singleton."""
    global _ncal
    if _ncal is not None:
        return _ncal
    with _ncal_lock:
        if _ncal is None:
            _ncal = NeuralConfidenceCalibrator.load()
        return _ncal
