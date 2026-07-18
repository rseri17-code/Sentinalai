# CHAOS_INVESTIGATION_REPORT.md
**SentinelAI — Production Readiness Certification · Phase 4**
Investigation-level chaos (not infrastructure chaos): degraded, conflicting, and
malformed *evidence*. Assessed from the code's handling + existing tests; gaps where no
chaos harness was executed are marked NOT MEASURED.

## Scenarios & handling
| Chaos condition | Code behaviour | Evidence | Status |
|---|---|---|---|
| Source unavailable (Splunk/Dynatrace/CMDB down) | `_call_worker` returns `{"error":...}`; investigation continues on remaining evidence | agent.py:1341 | HANDLED (crash-safe) |
| Missing logs / partial traces | evidence key absent; downstream extractors find nothing → lower confidence; Evidence Gate G1/G4 can BLOCK to "insufficient evidence" | collect.py:288 | HANDLED |
| Empty incident / meta-query | early-return `_empty_result` / `META_QUERY_NOT_INCIDENT` | fetch.py:153-190 | HANDLED |
| Malformed / non-JSON worker response | wrapped `{"raw_response":...}`, not flagged error → **silent evidence loss** | mcp_client.py:766 | PARTIAL (silent) |
| Contradictory evidence | T2 `detect_contradictions` (CrossSourceConflict) + T4 `contradicting_evidence` lowers validation score + status can become `contradicts` | adaptive/validation engines | HANDLED (shadow-only signal) |
| Duplicated evidence / alerts | dedup via `dict.fromkeys` (order-preserving); MCP dedup signature `sort_keys=True` | collect.py:276, mcp_client.py:681 | HANDLED |
| Out-of-order / delayed timestamps | T2 `check_temporal_causality`, T3 temporal chain elimination | causal engine | HANDLED (shadow-only signal) |
| Wrong topology (victim blamed) | T3 topology-victim rejection (`topology_possible=False`) | causal_investigation | HANDLED (shadow-only signal) |
| Conflicting metrics / cross-system inconsistency | surfaced in evidence; no authoritative reconciliation (shadow signals only) | — | PARTIAL |
| Race conditions (parallel workers) | keyed dict writes are order-independent, BUT timing-based future cancellation can change the evidence **set** (D-5) | agent.py:1986-2011 | RISK |

## Does RCA drift under chaos?
- **Given a fixed evidence set:** no — the reasoning engine is byte-deterministic
  (1000/1000 identical, DETERMINISM_REPORT).
- **Under evidence-set chaos:** **yes, possibly** — the timing-based parallel-playbook
  cancellation (D-5) and the wall-clock change-window (D-1) can change *which* evidence is
  collected, which can move the RCA. This is the core chaos exposure.

## Does confidence stay calibrated under chaos?
NOT MEASURED at scale (N=3 corpus, calibration underpowered). The mechanism is sound —
missing/contradictory evidence lowers `evidence_validation_score` and can flip
verification status to `insufficient`/`contradicts` — but this is a shadow signal today,
not an authoritative confidence adjustment.

## Does replay still work under chaos?
Yes for the *reasoning* (replay pins the evidence and re-runs deterministic analysis).
BUT the replay **artifact** is not canonicalised (D-3), so byte-identity of the stored
artifact is not guaranteed. Replay correctness (same RCA) holds; replay artifact
byte-stability does not.

## NOT MEASURED (no chaos harness executed)
- Systematic fault-injection across all 20 chaos conditions with pass/fail assertions.
- RCA-drift rate under randomised evidence dropout.
- Confidence-calibration stability under injected contradictions at scale.
These require a deterministic investigation-chaos harness (backlog C-4) — the single
biggest evidence gap for chaos certification.

## Verdict
**CONDITIONALLY CERTIFIED.** The system degrades gracefully and never crashes under
evidence chaos, and the shadow engines add strong contradiction/temporal/topology
handling. But two live-path defects (D-1 wall-clock window, D-5 timing-based evidence-set
cancellation) mean RCA *can* drift under chaos, and no systematic chaos harness has
quantified the drift rate. Fix D-1/D-5, then build the chaos harness, before chaos
resilience can be CERTIFIED.
