# PRODUCTION_BLOCKER_CLOSURE.md
**SentinelAI — Production Readiness Closure Program**
Commit `56129ef` · Full regression **5682 passed, 2 skipped, 0 failed** · No new capabilities.

Closes every verified blocker from `EXECUTIVE_READINESS_SUMMARY.md`. Each change traces
to exactly one documented blocker; no refactoring, no optimization, no architecture change.

---
## 1. Production Blocker Closure Report
| Blocker | Root cause | Fix | Verified |
|---|---|---|---|
| **B-1** wall-clock change window | `_get_change_time_window` (agent.py) computed `elapsed = datetime.now() − created_at` → replay queried a different change window | Anchored to **immutable incident timestamps only** (`created_at` + first of `detected_at/resolved_at/closed_at/updated_at/end_time`) via new `_parse_incident_ts`; deterministic 24h default when no end timestamp | 5 unit tests; `datetime.now` removed from source; window identical across wall-clock advance |
| **B-2** wall-clock `incident_hour` | `_build_dna_evidence_dict` set `incident_hour = now().hour` → fed DNA fingerprint → hypothesis priming | Derived from the incident timestamp | 4 unit tests; repo-wide scan (below) confirms no other FORBIDDEN clock use |
| **B-3** non-canonical serialization | `replay.save` + `RCAReport.to_json` used `json.dumps(...)` without `sort_keys` | Added `sort_keys=True` to both | 1000-iteration byte-identity certification (1 hash) with alternating key order |
| **F-obs** silent failures | two future-await swallows in CollectPhase logged nothing; no aggregated view | Both now log at WARNING + record structured entries; post-collection sweep folds worker `{"error":...}` into `evidence["_sources_unavailable"]`; AnalyzePhase lifts to `result["_sources_unavailable"]` + `degraded_investigation=True` | 3 unit tests (record/dedup, worker scan, determinism) |

## 2. Determinism Re-Certification
- **B-1/B-2:** the two verified `datetime.now()` sites that influenced investigation
  behaviour are removed; both now derive from immutable incident timestamps.
- **Reasoning engine:** unchanged, remains byte-deterministic (1000/1000, prior cert).
- **Result:** no ambient wall-clock behaviour influences root_cause / confidence /
  evidence-selection / hypothesis-ranking on the live path.

## 3. Replay Re-Certification
- Replay artifact + RCA report now serialize canonically.
- **1000 iterations, alternating key-insertion order → 1 identical sha256** for both
  the replay artifact and the RCA report. Byte-identical persistence + stable hashes.
- Replay *correctness* (same evidence → same RCA) was already certified; artifact
  *byte-stability* is now certified too.

## 4. Observability Certification
- **Logs:** every unavailable evidence source (experience_store, knowledge_graph,
  any worker `{"error":...}`) is logged at WARNING (was silent for 2 of them).
- **Shadow metadata / report:** `result["_sources_unavailable"]` (list of
  `{source, reason}`) + `result["degraded_investigation"]` flag surface degraded runs.
- **Receipts:** worker-level failures were already recorded to receipts +
  tool_transparency by `_call_worker` (unchanged; verified).
- Operators can now distinguish "low confidence from clean evidence" from
  "low confidence because N sources were down."
- *Deferred (documented, not silent):* the optimistic `_ALL_KNOWN_SERVERS`
  discovery fallback (`mcp_client.py:975`) still masks a fully-down gateway at
  discovery time — logged as WARNING today; converting it to a hard "discovery
  degraded" signal is tracked as a P1 follow-up (out of scope for the four blockers).

## 5. Repository Non-Determinism Audit (Phase 3)
Whole-repo scan of the runtime investigation path for `datetime.now/utcnow`, `time.time`,
`random`, `uuid`, non-canonical `json.dumps`, set-ordering, salted `hash()`:

> **FORBIDDEN: none.** After B-1/B-2, no runtime non-determinism reaches root_cause /
> confidence / evidence-selection / hypothesis-ranking or any live (default-ON) hashed
> or persisted artifact.

- **SHADOW-ONLY** (default-OFF `INVESTIGATION_ARTIFACT_ENABLED`): artifact-builder
  hashes phase-receipt timings into `artifact_id` — inert on the live path.
- **SAFE** (verified, not defects): DNA fingerprint hashes only structural features
  (`encoded_at` excluded); `recurrence.days_since_last` not used by grounding;
  `source_confidence` freshness decay is on an unwired path; `time.monotonic()`
  deadlines are timing side-channels; uuid/correlation ids never enter the result;
  post-result learning-store timestamps are membership/similarity inputs, not
  load-bearing; seeded RNG is deterministic.

## 6. Regression Report
`pytest -q` → **5682 passed, 2 skipped, 0 failed** (13:38). Prior baseline 5665 + 17 new
closure tests. Impact zone (stabilization/replay/integration/synthetic/hypothesis + supervisor
+ investigate-core + phase-receipts): 530 passed.

## 7. Remaining Blockers
**None of the four certification blockers remain.** Remaining items are the pre-existing
P1/P2/P3 backlog (load/concurrency harness C-1, learning-store drift characterisation C-2,
fault-injection harness C-3, labeled corpus V-1, SentinelBench-live V-4, tech-debt T-*),
plus the one deferred observability hardening noted in §4. None is a determinism or
safety defect.

## 8. Rollback
Single focused commit `56129ef`; `git revert 56129ef` restores prior behaviour. Each fix
is independently revertible (B-1/B-2 in `_get_change_time_window`/`_build_dna_evidence_dict`
+ helper; B-3 two one-line serializer args; F-obs additive helpers + a lifted metadata key).
No schema, storage, or contract migration.

## 9. Final Release Recommendation
> ### READY FOR 90-DAY SHADOW PILOT (read-only)

All four verified production blockers are closed, determinism and replay are re-certified,
no ambient wall-clock influences investigations, dependency failures are operator-visible,
and regression is clean. Runtime authority, Wave 3, and runtime retrieval **remain OFF** —
governed by the existing readiness program and the longitudinal shadow evidence still to be
accumulated. Proceed to the read-only shadow pilot.
