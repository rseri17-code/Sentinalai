"""R2 Part A — evidence-grounded confidence. Failing-first + acceptance.

Proves and closes the verified double-count: `source_count` (category presence)
and `corroborating_sources` (= len(evidence_refs), findings in those same
categories) both credited the same source. After the fix, corroboration is
counted once per source; every contribution is attributable via
confidence_provenance().
"""
from __future__ import annotations

from supervisor.helpers.confidence import (
    compute_confidence,
    confidence_provenance,
)


def _ev():
    return dict(base=50.0, logs=[{"m": "x"}],
                signals={"golden_signals": {"latency": 1},
                         "anomaly_detected": True},
                metrics={"metrics": {"cpu": 1}}, events=[], changes=[],
                incident_type="error_spike")


# ---------------------------------------------------------------------------
# Part A — no double count
# ---------------------------------------------------------------------------

class TestNoDoubleCount:
    def test_refs_to_present_sources_add_nothing(self):
        # refs pointing at already-present categories must not inflate again
        e = _ev()
        c0 = compute_confidence(e["base"], e["logs"], e["signals"],
                                e["metrics"], e["events"], e["changes"],
                                corroborating_sources=0,
                                incident_type=e["incident_type"])
        c2 = compute_confidence(e["base"], e["logs"], e["signals"],
                                e["metrics"], e["events"], e["changes"],
                                corroborating_sources=2,
                                incident_type=e["incident_type"])
        assert c2 == c0        # was c0 + 4 before the fix (double count)

    def test_corroborating_sources_never_inflates(self):
        e = _ev()
        vals = {compute_confidence(e["base"], e["logs"], e["signals"],
                                   e["metrics"], e["events"], e["changes"],
                                   corroborating_sources=k,
                                   incident_type=e["incident_type"])
                for k in range(0, 6)}
        assert len(vals) == 1    # confidence independent of ref COUNT


# ---------------------------------------------------------------------------
# Part A — provenance: every contribution appears exactly once and sums
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_components_sum_to_final(self):
        e = _ev()
        prov = confidence_provenance(
            e["base"], e["logs"], e["signals"], e["metrics"], e["events"],
            e["changes"], incident_type=e["incident_type"])
        total = prov["base"] + sum(c["delta"] for c in prov["contributions"])
        assert int(round(max(0, min(100, total)))) == prov["final_confidence"]

    def test_final_matches_compute_confidence(self):
        e = _ev()
        prov = confidence_provenance(
            e["base"], e["logs"], e["signals"], e["metrics"], e["events"],
            e["changes"], incident_type=e["incident_type"])
        assert prov["final_confidence"] == compute_confidence(
            e["base"], e["logs"], e["signals"], e["metrics"], e["events"],
            e["changes"], incident_type=e["incident_type"])

    def test_each_source_attributed_once(self):
        e = _ev()
        prov = confidence_provenance(
            e["base"], e["logs"], e["signals"], e["metrics"], e["events"],
            e["changes"], incident_type=e["incident_type"])
        # corroboration line items name a source; no source appears twice
        srcs = [c["source"] for c in prov["contributions"]
                if c["kind"] == "corroboration"]
        assert len(srcs) == len(set(srcs))

    def test_deterministic(self):
        e = _ev()
        a = confidence_provenance(e["base"], e["logs"], e["signals"],
                                  e["metrics"], e["events"], e["changes"],
                                  incident_type=e["incident_type"])
        b = confidence_provenance(e["base"], e["logs"], e["signals"],
                                  e["metrics"], e["events"], e["changes"],
                                  incident_type=e["incident_type"])
        assert a == b

    def test_absent_sources_penalized_once(self):
        # no signals/metrics for a type where presence is expected → penalties
        prov = confidence_provenance(50.0, [{"m": "x"}], {}, {}, [], [],
                                     incident_type="error_spike")
        penalties = [c for c in prov["contributions"] if c["delta"] < 0]
        assert penalties      # penalties appear
        # each penalty appears once
        assert len({c["source"] for c in penalties}) == len(penalties)
