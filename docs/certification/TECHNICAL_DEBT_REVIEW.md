# TECHNICAL_DEBT_REVIEW.md
**SentinelAI — Production Readiness Certification · Phase 11**
Evidence-based. Architecture is LOCKED — every item is decompose/delete/consolidate
*within* the existing design, never a redesign.

## Dead / unwired code
| Item | Evidence | Verdict |
|---|---|---|
| `intelligence/confidence_calibrator.py` (216 LOC) | **Zero runtime importers**; imported only by its test (19×). Duplicate concept of `supervisor/confidence_calibrator.py` (10+ runtime importers). | **HIGH — dead module. Delete candidate.** |
| `agents/`, `skills/`, `tasks/` | 0 Python files (`.md` agent/skill specs + CLAUDE.md memory store). Not read by `investigate()`. | Not runtime debt — docs/memory. Leave. |
| `ui/` (React/TS) | 0 `.py`; served only by `agui`. | Separate frontend. Not backend debt. |
| `agui/` (38 py) | Imported lazily under `if AGUI_ENABLED`. | Live-but-optional. Not dead. |

## Feature flags (59 unique `_ENABLED`)
- **51 default-ON, 5 default-OFF, shadow family default-OFF.**
- **Shadow-awaiting-promotion (do NOT cut):** `HYPOTHESIS_ENGINE_ENABLED`,
  `ADAPTIVE_INVESTIGATION_ENABLED`, `CAUSAL_INVESTIGATION_ENABLED`,
  `VALIDATION_ENGINE_ENABLED`, `DECISION_INTELLIGENCE_ENABLED`,
  `EVIDENCE_LEDGER_SHADOW_ENABLED` — additive `_*`-only, wired at
  `analyze.py:306-351`. These are the certification subject; keep. **If the
  promotion roadmap is abandoned they become HIGH dead-weight** — re-confirm intent.
- **Permanently-ON (~10, e.g. `LLM_ENABLED`, `OPS_DB_ENABLED`, `SLO_ENABLED`):** dead
  config surface, harmless. LOW — prune to shrink the flag surface.
- **Single-module default-ON toggles** (`CRITIQUE_LLM_ENABLED`, `DEV_LOOP_ENABLED`,
  `REVIEW_RESPONDER_ENABLED`, `REGRESSION_ENABLED`, `HONEYPOT_ENABLED`): MED — confirm
  reachability from `investigate()` vs CI/agui-only. `EVIDENCE_GATES_ENABLED` aliases to
  internal `GATES_ENABLED` — verify the alias is consumed (naming-mismatch risk).

## Duplicate logic (HIGH — determinism-relevant)
| Helper | Copies | Locations |
|---|---|---|
| `_tokens` | 4 | hypothesis_engine:65, intel_memory/similarity:39, investigation_value/metrics:20, scientific_validation:65 |
| `_jaccard` | 3 | blast_radius_history:59, similarity:50, metrics:32 |
| `_clamp` | 4 (divergent signatures) | hypothesis_engine:74, neural_quality_net:379, hypothesis_tracker:195, metrics:38 |
| `_round` | 3 | decision_intelligence:49, validation_engine:61, scientific_validation:61 |
| `_redact_params` | 2 (redaction-drift = security risk) | receipt:167, models/receipts:71 |
| sha256 id-gen | **31 files** | no shared `stable_id()` — the platform's core guarantee is spread across 31 call sites |

## Hidden coupling — cross-package private imports (MED)
9 sites import `_underscore` internals across package boundaries (e.g.
`from sentinel_core.investigation_value.metrics import _jaccard, _tokens` ×3,
`from supervisor.hypothesis_engine import _tokens`,
`from sentinel_core.models.receipts import _redact_params`). Each silently breaks if the
owner refactors. Promote to public API or relocate.

## Complexity hotspots (MED/HIGH)
- `supervisor/agent.py` = **3,799 LOC**. Worst functions: `_persist_results` **458 LOC**
  (`agent.py:735`), `_analyze_evidence` 296 LOC, `_apply_self_critique` 167 LOC,
  `investigate` 159 LOC, `_execute_playbook` 152 LOC.
- Next: `workers/mcp_client.py` (1,289), `agui/api/intake.py` (829),
  `agent_harness.py` (820), `database/ops_persistence.py` (779), `tool_selector.py` (727).

## Prioritized cleanup backlog
**HIGH**
1. Delete/merge `intelligence/confidence_calibrator.py` (dead, test-only orphan).
2. Consolidate `_tokens`/`_jaccard`/`_clamp`/`_round` into one shared util (also fixes
   coupling). ⚠️ divergent `_clamp` signatures — reconcile carefully, re-run full suite.
3. One canonical deterministic `stable_id()` for the 31 sha256 id sites (the product's
   core guarantee should have one implementation).
4. Decompose `_persist_results` (458 LOC) and thin `agent.py` (3,799 LOC) *within* the
   module — architecture LOCKED, so extract helpers, do not redesign.

**MED**
5. De-duplicate `_redact_params` (redaction drift is a data-safety risk).
6. Remove the 9 cross-package private imports.
7. Audit the single-module default-ON toggles for reachability.

**LOW**
8. Prune ~10 permanently-ON flags to shrink the 59-flag surface.
9. Keep the 6 shadow flags; re-confirm the promotion roadmap is alive.

## Debt severity summary
No debt item is a **correctness or safety defect** — the system is regression-clean at
5665 tests. The debt is **maintainability risk** (duplication, one oversized module,
private coupling) that raises the cost and risk of *future* change. Retire HIGH items
before expanding capabilities (Wave 3), not before a read-only pilot.
