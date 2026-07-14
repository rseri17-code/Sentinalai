# Canonical Observation Schema
**Deliverable 2 · The immutable record every investigation contributes**

Produced by `observation_record(result, incident, *, commit, model, observed_period,
replay_hash, label)`. Immutable, deterministic id, JSON-safe, composed only from existing
outputs. No wall-clock — investigation fields derive from incident timestamps only.

```jsonc
{
  "schema_version": 1,
  "record_id": "<sha256[:16] of the record minus record_id>",   // deterministic
  "incident_id": "INC-...",
  "observed_period": "2026-W09",        // caller-supplied bucket key (not a clock read)
  "commit": "23e8b28",                  // repository version
  "model": "opus",                      // model version
  "shadow_versions": { "validation": 1 },
  "feature_flags": {                    // which shadow engines were ON
    "hypothesis_engine": true, "adaptive": true, "causal": true,
    "validation": true, "decision_intelligence": true
  },
  "determinism_hash": "<sha256[:16] of core>",   // identical inputs → identical hash
  "replay_hash": "<caller-supplied replay artifact hash>",
  "core": {
    "root_cause": "...", "confidence": 80,
    "incident_type": "saturation", "service": "payment", "severity": 2,
    "citation_coverage": 0.9,
    "evidence_validation_score": 0.85,       // T4
    "evidence_confidence": 78,               // T4 reconstruction
    "verification_status": "supports",       // T4 status ladder
    "shadow_independent_winner": "...",       // T5 arbitration winner
    "decision_stable": true,                  // T5 stability
    "decision_quality": 0.85,                 // T5
    "hypothesis_count": 3,                     // T1
    "localization_service": "payment",         // T3
    "investigation_completeness": 0.83,        // T4
    "expert_concordance": 1.0,                 // T4
    "sources_unavailable": [{"source":"knowledge_graph","reason":"timeout"}],  // F-obs
    "degraded_investigation": false,           // F-obs
    "worker_failures": 0
  },
  "label": {                            // Phase-2 operator label (optional)
    "verdict": "ROOT_CAUSE_CORRECT|PARTIAL|INCORRECT|UNKNOWN",
    "labeled": true,
    "validated_root_cause": "...", "actual_remediation": "...",
    "resolution_time_ms": 3600000, "operator_confidence": 90,
    "operator_comments": "...", "false_positive": false, "false_negative": false,
    "missing_evidence": []
  }
}
```

## Field provenance
Every `core` field is lifted verbatim from an existing produced key — nothing is
recomputed. `determinism_hash` hashes only `core` (stable given identical inputs);
`record_id` hashes the whole record minus itself. Both are deterministic and replayable.

## Invariants (enforced by tests)
- Same `(result, incident, commit, model, period, label)` → same `record_id`.
- No `datetime.now`/`time.time` in the builder (verified by source inspection).
- `label.labeled` is False unless an operator verdict other than UNKNOWN is supplied.
- JSON round-trips byte-identically.
