"""Pure-Python MLP for investigation quality prediction.

Replaces the fixed-weight heuristics in OnlineEvaluator with a learned
2-layer network that updates via backpropagation after every investigation.
No external ML dependencies — implemented with the Python standard library only.

Architecture:  9 inputs → 16 hidden (ReLU) → 8 hidden (ReLU) → 1 output (sigmoid)
Optimizer:     SGD with Nesterov momentum  (lr=0.01, momentum=0.9)
Training:      Online — one gradient step per completed investigation
Persistence:   eval/neural_quality_net.json  (atomic write)

Feature vector (9 dimensions, all in [0, 1]):
  0  volume        evidence_volume from OnlineEvaluator
  1  coherence     evidence_coherence from OnlineEvaluator
  2  calibration   confidence_calibration from OnlineEvaluator
  3  specificity   root_cause_specificity from OnlineEvaluator
  4  diversity     hypothesis_diversity from OnlineEvaluator
  5  raw_conf      raw_confidence / 100
  6  has_logs      1 if "logs" source category found
  7  has_metrics   1 if "metrics" source category found
  8  has_signals   1 if "signals" source category found

Target:
  Ground truth  → actual_correct ∈ {0, 1}   (hard label from GroundTruthEvaluator)
  Pseudo-label  → online_quality_score ∈ [0, 1]  (soft label when GT unavailable)

Blending:
  The model blends with the heuristic OnlineEvaluator score.  The neural
  contribution grows linearly from 0% at 0 samples to MAX_BLEND_WEIGHT (40%)
  at MIN_SAMPLES_FOR_BLEND (30) samples, then stays flat.  This ensures the
  heuristic remains dominant until the network has enough data to be useful.

Usage:
    from supervisor.neural_quality_net import get_quality_net, build_features

    net = get_quality_net()
    features = build_features(dims, raw_confidence, sources_found)
    score = net.predict(features)          # inference
    net.train_one(features, target=1.0)    # online update
    net.save()
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import threading

logger = logging.getLogger("sentinalai.neural_quality_net")

_MODEL_PATH = os.environ.get(
    "NEURAL_QUALITY_MODEL_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "neural_quality_net.json"),
)

# Blending knobs
MIN_SAMPLES_FOR_BLEND: int = 30   # neural output gets 0 weight below this
MAX_BLEND_WEIGHT: float = 0.40    # max fraction contributed by neural model

# Training knobs
_LR: float = 0.01
_MOMENTUM: float = 0.90
_GRAD_CLIP: float = 5.0           # per-parameter gradient clip
_LAYER_SIZES: list[int] = [9, 16, 8, 1]


# ---------------------------------------------------------------------------
# Core pure-Python MLP
# ---------------------------------------------------------------------------

class _MLP:
    """Minimal pure-Python multi-layer perceptron.

    Hidden layers: ReLU.  Output layer: sigmoid.
    Optimizer: SGD with Nesterov momentum.
    All weights stored as nested Python lists for JSON serializability.
    """

    def __init__(
        self,
        layer_sizes: list[int],
        lr: float = _LR,
        momentum: float = _MOMENTUM,
        seed: int = 0,
    ) -> None:
        rng = random.Random(seed)
        self.layer_sizes = layer_sizes
        self.lr = lr
        self.momentum = momentum
        self.n_layers = len(layer_sizes) - 1
        self.total_samples: int = 0

        self.W: list[list[list[float]]] = []
        self.b: list[list[float]] = []
        self.vW: list[list[list[float]]] = []  # momentum buffers
        self.vb: list[list[float]] = []

        for i in range(self.n_layers):
            n_in, n_out = layer_sizes[i], layer_sizes[i + 1]
            # He initialization: std = sqrt(2 / n_in)
            std = math.sqrt(2.0 / n_in)
            W = [[rng.gauss(0.0, std) for _ in range(n_in)] for _ in range(n_out)]
            b = [0.0] * n_out
            self.W.append(W)
            self.b.append(b)
            self.vW.append([[0.0] * n_in for _ in range(n_out)])
            self.vb.append([0.0] * n_out)

    # ------------------------------------------------------------------
    # Activations — Leaky ReLU prevents dying neurons in deeper layers
    # ------------------------------------------------------------------

    _LEAK: float = 0.01

    @classmethod
    def _relu(cls, x: float) -> float:
        return x if x > 0.0 else cls._LEAK * x

    @classmethod
    def _relu_d(cls, x: float) -> float:
        return 1.0 if x > 0.0 else cls._LEAK

    @staticmethod
    def _sigmoid(x: float) -> float:
        x = max(-50.0, min(50.0, x))
        return 1.0 / (1.0 + math.exp(-x))

    # ------------------------------------------------------------------
    # Forward pass
    # ------------------------------------------------------------------

    def _forward(
        self, x: list[float]
    ) -> tuple[list[list[float]], list[list[float]]]:
        """Return (pre_activations, activations) per layer."""
        pre_acts: list[list[float]] = []
        activations: list[list[float]] = [x]

        for i in range(self.n_layers):
            W, b = self.W[i], self.b[i]
            h_in = activations[-1]
            z = [
                sum(W[j][k] * h_in[k] for k in range(len(h_in))) + b[j]
                for j in range(len(b))
            ]
            pre_acts.append(z)
            # Last layer → sigmoid; hidden layers → ReLU
            if i == self.n_layers - 1:
                activations.append([self._sigmoid(z[0])])
            else:
                activations.append([self._relu(v) for v in z])

        return pre_acts, activations

    def predict(self, x: list[float]) -> float:
        """Forward pass → scalar prediction in [0, 1]."""
        _, acts = self._forward(x)
        return acts[-1][0]

    # ------------------------------------------------------------------
    # Backward pass (SGD + Nesterov momentum)
    # ------------------------------------------------------------------

    def train_one(self, x: list[float], target: float) -> float:
        """One SGD step. Returns MSE loss (before update)."""
        pre_acts, acts = self._forward(x)
        y_hat = acts[-1][0]
        loss = (y_hat - target) ** 2

        # Output layer delta: d(MSE)/d(pre_out) = 2*(y_hat-target) * sigmoid'(z)
        # sigmoid'(z) = y_hat*(1-y_hat)  when y_hat = sigmoid(z)
        out_delta = [2.0 * (y_hat - target) * y_hat * (1.0 - y_hat)]
        deltas: list[list[float]] = [out_delta]

        # Backprop through hidden layers
        for i in range(self.n_layers - 2, -1, -1):
            W_next = self.W[i + 1]
            delta_next = deltas[0]
            z_this = pre_acts[i]
            delta_this = [
                sum(W_next[k][j] * delta_next[k] for k in range(len(delta_next)))
                * self._relu_d(z_this[j])
                for j in range(len(z_this))
            ]
            deltas.insert(0, delta_this)

        # Nesterov momentum weight update with gradient clipping
        for i in range(self.n_layers):
            h_in = acts[i]
            delta = deltas[i]
            for j in range(len(delta)):
                for k in range(len(h_in)):
                    g = max(-_GRAD_CLIP, min(_GRAD_CLIP, delta[j] * h_in[k]))
                    self.vW[i][j][k] = self.momentum * self.vW[i][j][k] - self.lr * g
                    self.W[i][j][k] += self.vW[i][j][k]
                gb = max(-_GRAD_CLIP, min(_GRAD_CLIP, delta[j]))
                self.vb[i][j] = self.momentum * self.vb[i][j] - self.lr * gb
                self.b[i][j] += self.vb[i][j]

        self.total_samples += 1
        return loss

    def train_batch(self, samples: list[tuple[list[float], float]]) -> float:
        """Train on a list of (features, target) pairs. Returns mean loss."""
        if not samples:
            return 0.0
        total = sum(self.train_one(x, y) for x, y in samples)
        return total / len(samples)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "layer_sizes": self.layer_sizes,
            "lr": self.lr,
            "momentum": self.momentum,
            "total_samples": self.total_samples,
            "W": self.W,
            "b": self.b,
            "vW": self.vW,
            "vb": self.vb,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_MLP":
        obj = cls(
            layer_sizes=d["layer_sizes"],
            lr=d.get("lr", _LR),
            momentum=d.get("momentum", _MOMENTUM),
        )
        obj.W = d["W"]
        obj.b = d["b"]
        obj.vW = d.get("vW", [[[0.0] * len(obj.W[i][0]) for _ in obj.W[i]] for i in range(obj.n_layers)])
        obj.vb = d.get("vb", [[0.0] * len(obj.b[i]) for i in range(obj.n_layers)])
        obj.total_samples = d.get("total_samples", 0)
        return obj


# ---------------------------------------------------------------------------
# NeuralQualityNet  —  wrapper with save/load + blend logic
# ---------------------------------------------------------------------------

class NeuralQualityNet:
    """Wraps _MLP with feature engineering, persistence, and blend logic."""

    def __init__(self, mlp: _MLP | None = None) -> None:
        self._mlp = mlp or _MLP(_LAYER_SIZES)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def total_samples(self) -> int:
        return self._mlp.total_samples

    def blend_alpha(self) -> float:
        """Weight for neural output in [0, MAX_BLEND_WEIGHT].
        Grows linearly until MIN_SAMPLES_FOR_BLEND, then holds flat.
        """
        if self._mlp.total_samples < 5:
            return 0.0
        frac = min(1.0, self._mlp.total_samples / MIN_SAMPLES_FOR_BLEND)
        return frac * MAX_BLEND_WEIGHT

    def predict(self, features: list[float]) -> float:
        """Return quality prediction in [0, 1].  Thread-safe."""
        with self._lock:
            return self._mlp.predict(features)

    def train_one(self, features: list[float], target: float) -> float:
        """One online SGD step. Returns MSE loss. Thread-safe."""
        with self._lock:
            return self._mlp.train_one(features, target)

    def train_batch(self, samples: list[tuple[list[float], float]]) -> float:
        """Batch training. Thread-safe."""
        with self._lock:
            return self._mlp.train_batch(samples)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | None = None) -> None:
        path = path or _MODEL_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 1, "model": self._mlp.to_dict()}, f)
        os.replace(tmp, path)
        logger.debug(
            "NeuralQualityNet saved: samples=%d path=%s", self._mlp.total_samples, path
        )

    @classmethod
    def load(cls, path: str | None = None) -> "NeuralQualityNet":
        path = path or _MODEL_PATH
        try:
            with open(path) as f:
                data = json.load(f)
            mlp = _MLP.from_dict(data["model"])
            logger.info(
                "NeuralQualityNet loaded: samples=%d path=%s", mlp.total_samples, path
            )
            return cls(mlp)
        except FileNotFoundError:
            logger.debug("NeuralQualityNet not found at %s — fresh model", path)
            return cls()
        except Exception as exc:
            logger.warning("NeuralQualityNet load failed (%s) — fresh model", exc)
            return cls()

    def get_report(self) -> dict:
        return {
            "total_samples": self._mlp.total_samples,
            "blend_alpha": round(self.blend_alpha(), 4),
            "active": self._mlp.total_samples >= MIN_SAMPLES_FOR_BLEND,
            "min_samples_for_blend": MIN_SAMPLES_FOR_BLEND,
            "max_blend_weight": MAX_BLEND_WEIGHT,
            "layer_sizes": self._mlp.layer_sizes,
            "lr": self._mlp.lr,
            "momentum": self._mlp.momentum,
        }


# ---------------------------------------------------------------------------
# Feature engineering
# ---------------------------------------------------------------------------

def build_features(
    dims: dict[str, float],
    raw_confidence: int,
    sources_found: list[str],
) -> list[float]:
    """Build the 9-dimensional feature vector from OnlineEvaluator output."""
    return [
        float(dims.get("volume", 0.0)),
        float(dims.get("coherence", 0.0)),
        float(dims.get("calibration", 0.0)),
        float(dims.get("specificity", 0.0)),
        float(dims.get("diversity", 0.0)),
        min(1.0, max(0.0, raw_confidence / 100.0)),
        1.0 if "logs" in sources_found else 0.0,
        1.0 if "metrics" in sources_found else 0.0,
        1.0 if "signals" in sources_found else 0.0,
    ]


def build_features_from_result(result: dict) -> list[float] | None:
    """Extract features from a completed investigation result dict.

    Returns None if the result lacks the _online_eval annotation
    (e.g. if online evaluation was disabled).
    """
    oe = result.get("_online_eval")
    if not oe:
        return None
    dims = oe.get("dimensions", {})
    sources_found = oe.get("sources_found", [])
    raw_conf = int(result.get("raw_confidence", result.get("confidence", 0)))
    return build_features(dims, raw_conf, sources_found)


# ---------------------------------------------------------------------------
# Process-level singleton
# ---------------------------------------------------------------------------

_net: NeuralQualityNet | None = None
_net_lock = threading.RLock()


def get_quality_net() -> NeuralQualityNet:
    """Return the process-level singleton, loading from disk on first call."""
    global _net
    if _net is not None:
        return _net
    with _net_lock:
        if _net is None:
            _net = NeuralQualityNet.load()
        return _net
