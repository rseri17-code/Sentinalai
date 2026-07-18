# SentinelAI — Claim Restoration Program (Mission Alpha)
**Objective:** make *"SentinelAI investigations are deterministic, replayable, and
evidence-grounded"* objectively true from source. **This document is diagnosis +
specification only — NO code was changed.** Ground truth: TRUTH_RECONCILIATION (`d2a9061`).

Every edge/store below is classified from source; **no edge is UNKNOWN.**

---
## 1. Root Cause Restoration Report
Two roots produce all restoration blockers:
- **R1 — corpus mutation feeds the authoritative result.** Five persisted learning stores are
  both READ during `investigate()` and WRITTEN after it, so run N mutates what run N+1 reads →
  non-deterministic result and non-hermetic replay.
- **R2 — confidence double-counts corroboration.** `compute_confidence` credits the same
  evidence source twice.
Everything else (evidence loss) is observability, not correctness of the answer.

---
## 2. Investigation Dependency Graph (edges classified)
Legend: READ (consulted) · WRITE (appended) · MUTATE (in-place change of prior state) ·
CACHE · DERIVED (computed in-run) · IMMUTABLE (fixed input).

```
Incident (IMMUTABLE input, incident.created_at)
  └─DERIVED→ classification (classify_incident)            [pure]
       └─READ→ evolved_strategy.json (get_evolved_playbook 1898,
                should_skip_step 1907)                     [READ → selects/drops playbook steps]
            └─DERIVED→ Evidence set (workers via _execute_playbook)
                 · worker calls                            [READ external; timeout/retry]
                 · experience_future  ←READ experience_store.json (collect)
                 · kg_future          ←READ knowledge_graph.json (collect)
                 · historical_future  ←READ (historical context)
                 └─→ evidence dict (DERIVED; mutated in-place in collect)
  └─DERIVED→ _dna_features/_fingerprint (_build_dna_evidence_dict; incident-ts only, B-2 fixed)
       └─READ→ pattern_registry.json (get_registry().match 2270)
            └─DERIVED→ _suggested_root_causes / primed hypotheses
  └─Analyzer (_analyze_evidence)
       └─DERIVED→ hypotheses (base_score = compute_confidence(...))   [R2 double-count here]
            └─READ→ knowledge_graph via retrieve_similar (2373) → retrieval_boost (+≤10)
            └─READ→ calibration_map (get_calibrator().calibrate)      [gated OFF by default]
            └─DERIVED→ winner = argmax(base_score) (agent.py:2343)
                 └─→ root_cause, confidence (RETURNED)
  └─Replay (investigate replay=True)
       └─READ pinned evidence (IMMUTABLE, stored)          [good]
       └─re-runs _analyze_evidence → RE-READS pattern_registry + knowledge_graph (LIVE)  [R1 leak]
  └─Persist (post-run WRITES that mutate future reads)
       ├─MUTATE pattern_registry.json (record 1027; centroid running-avg + last_seen=now())
       ├─MUTATE evolved_strategy.json (record_outcome 960)
       ├─WRITE  experience_store.json (store_experience 936; timestamp=now())
       ├─MUTATE knowledge_graph.json (ingest 891)
       ├─WRITE  calibration_map / neural_confidence_calibrator (record 996)  [gated]
       ├─WRITE  blast_radius_history.json (1075)           [WRITE-only; not read on auth path]
       ├─WRITE  cascade_tracker.json (1102)                [WRITE-only]
       └─WRITE  episodic_memory (record 1191; uuid4+now())  [WRITE-only; side-channel]
  └─Artifacts (replay.py save; sort_keys=True, canonical — B-3 fixed)   [IMMUTABLE once written]
  └─Memory (Wave-3 MemoryStore)                            [produce-only, admission-gated OFF]
```

### Store participation table (complete — no UNKNOWN)
| Store (eval/*) | READ in investigate | WRITE after | On authoritative result? | Class |
|---|---|---|---|---|
| `pattern_registry.json` | ✅ match 2270 | ✅ record 1027 (MUTATE centroid) | **YES → root_cause** | **MUTATE — freeze** |
| `evolved_strategy.json` | ✅ 1898/1907 | ✅ 960 | **YES → evidence set** | **MUTATE — freeze** |
| `experience_store.json` | ✅ collect | ✅ 936 | **YES → primed causes** | **MUTATE — freeze** |
| `knowledge_graph.json` | ✅ 2373/collect | ✅ 891 | **YES → confidence+priming** | **MUTATE — freeze** |
| `calibration_map` / `neural_confidence_calibrator` | ✅ calibrate (gated) | ✅ 996 | YES only if `CALIBRATION_ENABLED` (default OFF) | **MUTATE — freeze when enabled** |
| `blast_radius_history.json` | ❌ | ✅ 1075 | no | WRITE-only observational |
| `cascade_tracker.json` | ❌ | ✅ 1102 | no | WRITE-only observational |
| `episodic_memory` | ❌ | ✅ 1191 | no | WRITE-only side-channel (uuid/now) |
| Wave-3 `MemoryStore` | ❌ (gated) | produce-only | no | isolated (two-gate) |

**Participate in runtime authority:** the top 4 (+ calibrator when on). **Observational only:**
the bottom 4. **Mutate investigations:** the top 4. **Must be frozen:** the top 4 (+ calibrator).
**Never consulted during replay:** the top 4 (replay must read only pinned evidence + a snapshot).

---
## 3. Frozen Corpus Specification (contract — do NOT implement)
The four authority-participating stores must be turned into **immutable, snapshotted inputs**
for the duration of one investigation, and pinned for replay.

**Contract:**
1. **Snapshot-at-entry:** at `investigate()` start, capture an immutable, content-hashed snapshot
   of `{pattern_registry, evolved_strategy, experience_store, knowledge_graph}` (+ calibration
   map if enabled). All in-run READs consult the snapshot, never the live file.
2. **No mid-investigation writes read back:** persist mutations (record/ingest/store) occur only
   at persist time and are **never** visible to the current investigation.
3. **Corpus version stamp:** the snapshot's content-hash (`corpus_version`) is recorded on the
   result + artifact, so a result is reproducible iff replayed against the same `corpus_version`.
4. **Determinism guarantee (restored):** *same incident + same corpus_version → byte-identical
   root_cause/confidence/evidence*, independent of run order or wall-clock.
5. **Wall-clock:** already removed from the read path (B-1/B-2); persist-time `now()` writes must
   never enter the snapshot.

---
## 4. Replay Isolation Specification (contract — do NOT implement)
Replay is currently non-hermetic: `investigate(replay=True)` re-runs `_analyze_evidence`, which
re-reads the **live** pattern registry + knowledge graph (agent.py:2270, 2373).

**Contract:**
1. Replay must consult the **`corpus_version` snapshot stored with the artifact**, never the live
   stores. If the snapshot is absent (legacy artifact), replay must **refuse or clearly degrade**,
   not silently read live state.
2. Replay must perform **zero writes** to any learning store.
3. Replay reads only: pinned evidence (already immutable) + the pinned corpus snapshot.
4. **Replay guarantee (restored):** replaying artifact A at any future time reproduces A's
   root_cause/confidence/evidence byte-for-byte. Artifact serialization is already canonical
   (`replay.py` sort_keys, B-3).

---
## 5. Confidence Provenance Graph (R2 — proof of double-count)
`base_score → compute_confidence(base, logs, signals, metrics, events, changes,
corroborating_sources=len(evidence_refs), incident_type)` (agent.py:2290-2292).

| Signal | Origin | Path | Contributes | Once? |
|---|---|---|---|---|
| base prior | hardcoded constant per hypothesis (e.g. 2727=80) | `base` | seed | n/a (constant, not evidence) |
| log presence | `logs` | `source_count+=1; score+=min(len,5)` + `source_count*2` (conf.py:47-49,64) | +2 (+≤5) | — |
| golden signals | `signals.golden_signals` | `source_count+=1 (+2 anomaly)` + `source_count*2` | +2..+4 | — |
| metrics | `metrics.metrics` | `source_count+=1` + `source_count*2` | +2 | — |
| events / changes | `events`/`changes` | `source_count+=1` + `source_count*2` | +2 each | — |
| **corroboration** | **`len(evidence_refs)`** where refs = `["golden_signals:latency","logs:timeout",...]` | **`corroborating_sources*2` (conf.py:74)** | **+2 per ref** | **NO — same sources as above** |
| retrieval boost | historical similarity (not current evidence) | `confidence += retrieval_boost` (≤+10, agent.py:2381) | +≤10 | disconnected |
| LLM override | model output | `base_score = r.get("score")` (agent.py:2511) | replaces score | disconnected |
| network delta | ThousandEyes evidence | `+total_confidence_delta*100` (2389) | grounded | once ✅ |
| calibration | calibration_map (gated OFF) | `calibrate()` rewrites | remap | once (gated) |

**Proof of duplication:** a `golden_signals` finding contributes **+2 via `source_count*2`
(line 64)** AND **+2 via `evidence_refs="golden_signals:..."` in `corroborating_sources*2`
(line 74)**. The two bonuses count the *same* underlying source. Therefore each signal does
**NOT** contribute exactly once.

**Smallest correction (spec only):** the two corroboration bonuses are redundant. Minimal fix =
**remove `score += corroborating_sources * 2` (confidence.py:74)** — `source_count*2` already
credits corroboration from actual data presence, and `evidence_refs` are frequently
asserted-not-verified strings. Secondary corrections (separate, smaller severity): gate/remove
`retrieval_boost` from confidence (it is historical similarity, not current evidence) and bound
the LLM override so it cannot exceed the evidence-derived ceiling. These three make confidence a
single-count function of current evidence.

---
## 6. Evidence Provenance Graph (every disappearance classified — no Unknown)
| Drop point | Location | Classification | Operator-visible? |
|---|---|---|---|
| worker error dict | `_call_worker` returns `{"error":...}` (agent.py ~1341) | **Error** | ✅ logged + receipts + tool_transparency |
| experience future fail | collect.py ~247 | **Filtered** | ✅ `_record_unavailable` → `_sources_unavailable` |
| kg future fail | collect.py ~292 | **Filtered** | ✅ `_record_unavailable` |
| worker `{"error"}` sweep | `_scan_worker_errors` | **Filtered** | ✅ `_sources_unavailable` |
| historical future fail | collect.py ~236 | **Dropped** | ❌ **log-only (warning)** — must be Filtered |
| tool_recs fail | collect.py ~278 | **Dropped** | ❌ **log-only (debug)** |
| trace correlation fail | collect.py ~400 | **Dropped** | ❌ **log-only (debug)** |
| visual evidence fail | collect.py ~420 | **Dropped** | ❌ **log-only (debug)** |
| malformed `{"raw_response"}` | mcp_client.py:766 | **Suppressed** | ❌ no `error` key → skipped by sweep |
| gateway discovery fail | mcp_client.py:975-1002 | **Suppressed** | ❌ returns "all connected" |
| worker-error overwrite | later success overwrites key before sweep | **Suppressed** | ❌ lost before sweep |
| gate-block before sweep | sweep runs after gate early-return | **Suppressed** | ❌ on block path |

**Restoration spec (do NOT implement):** the 8 ❌ rows must move to **Filtered** — route every
await-failure, `raw_response`, gateway-discovery failure, overwrite, and pre-block drop through
`_record_unavailable` so each becomes a visible `_sources_unavailable` entry. No evidence path may
remain **Dropped/Suppressed**; **Unknown is eliminated** (all 12 classified).

---
## 7. Claim Restoration Matrix
Current state (from source) and what each requires to become TRUE.
| Marketing claim | Current | Blocking source | Restores when |
|---|---|---|---|
| **Deterministic** | **FALSE** (auth path) / TRUE (engine) | pattern_registry 2270↔1027, strategy 1898↔960, experience, KG (R1) | Frozen Corpus Contract (§3) |
| **Replayable** | **PARTIALLY TRUE** (artifact canonical; result non-hermetic) | replay re-reads live stores (agent.py:2270/2373) | Replay Isolation (§4) |
| **Evidence-grounded** | **PARTIALLY TRUE** | confidence double-count (confidence.py:74) + retrieval boost (2381) + LLM override (2511) + hardcoded refs (2708) | R2 correction (§5) |
| **Auditable** | **PARTIALLY TRUE** | 8 silent evidence drops (§6); result reproducibility (R1) | §4 + §6 |
| **Learning** | **TRUE** | stores do learn (record/ingest/evolve) — this is real | already true; must be *isolated per-investigation*, not removed |

**The learning claim is genuinely TRUE** and must be preserved — the fix is to make learning
**between** investigations (snapshot per run) rather than **within/across** a single
investigation and its replay. Restoration does not remove learning; it quarantines its effect to
future runs.

---
## 8. Success criterion (what would flip the red-team verdict)
An independent red-teamer would be forced to change "determinism FALSIFIED / replay FALSIFIED" to
**HOLDS** once, from source: (a) all four authority stores are read from an entry-snapshot
(§3), (b) replay reads only pinned evidence + the pinned `corpus_version` and writes nothing
(§4), (c) `corroborating_sources*2` is removed and retrieval-boost/LLM-override are bounded (§5),
and (d) the 8 silent evidence drops become `_sources_unavailable` entries (§6). This report
specifies exactly those changes; **implementation is the next mission, not this one.**
