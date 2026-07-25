# SentinelAI — Product Readiness Audit (pre-README)

Documents SentinelAI **as implemented today**, so the README can be an accurate
technical specification — not a roadmap. Every claim is classified and
repository-referenced. No README text is written here.

Classifications: **IMPLEMENTED** · **PARTIALLY IMPLEMENTED** ·
**NOT YET VALIDATED** (needs pilot/powered data) · **FUTURE WORK** (present but
default-off / not built).

---

## Executive Summary

SentinelAI is a **deterministic, evidence-grounded RCA platform** with a real
FastAPI BFF + React investigation workspace, one wired operator-intelligence
surface, and a complete offline measurement/validation stack. Its integrity
properties (determinism, replay, evidence/confidence provenance) are
IMPLEMENTED and test-covered (**5,984 tests collected**). Its *operational
value* (MTTI reduction, operator trust) is **NOT YET VALIDATED** — no pilot has
run. By default it investigates **stub data** (`GATEWAY_MODE=stub`) and runs
**human-in-the-loop** (autonomy/agentic planner default-off). The README must
reflect exactly this: strong integrity, honest "not yet validated" on outcomes,
and a real gateway/auth configuration step before use.

## Repository Overview

- **~160,427 Python LOC** across 20 top-level packages (`supervisor/`,
  `sentinel_core/`, `agui/`, `intelligence/`, `workers/`, `integrations/`,
  `database/`, `knowledge/`, `eval/`, `tests/`, `ui/`, …).
- **UI:** React 18 + Vite + Tailwind SPA (`ui/`), 18 feature components
  (post-Phase-2), served from committed `ui/dist` by the BFF.
- **Tests:** 5,984 collected; last full run 5,982 passed / 2 skipped / 0 failed.

## Capability Inventory (classified)

| Capability | Evidence (repo) | Classification |
|---|---|---|
| Deterministic 5-phase investigation engine | `supervisor/phases/*`, `supervisor/agent.py::investigate` | IMPLEMENTED |
| R1 determinism + Frozen Corpus + Hermetic Replay | `supervisor/frozen_corpus.py`, `supervisor/replay.py` | IMPLEMENTED |
| Evidence provenance + lifecycle (R2) | `supervisor/phases/collect.py::_evidence_lifecycle`, `analyze.py` | IMPLEMENTED |
| Confidence provenance (single-count) | `supervisor/helpers/confidence.py::confidence_provenance` | IMPLEMENTED |
| Knowledge graph | `sentinel_core/models/knowledge_graph.py`, `intelligence/causal_graph.py` | IMPLEMENTED |
| BFF API + WebSocket streaming | `agui/main.py` (18 routers), `agui/ws_manager.py` | IMPLEMENTED |
| Investigation Workspace UX | `ui/src/components/InvestigationSummary`, tablist a11y (`Sidebar.tsx`), 2xl layout (`AppShell.tsx`) | IMPLEMENTED (Phase 2) |
| Alert intake (Moogsoft/PagerDuty/ServiceNow/Opsgenie/Grafana/CloudWatch) | `agui/api/intake.py` | IMPLEMENTED |
| Auth JWT/RBAC (secure default) | `agui/middleware/auth.py` (`AGUI_AUTH_REQUIRED` default true, import-time secret guard) | IMPLEMENTED |
| Operational Health (OIP #1) wired end-to-end | `sentinel_core/oip/operational_health.py` + `agui/api/operational_health.py` + `ui/.../OperationalHealth` | IMPLEMENTED |
| MTTI instrumentation (system + operator) | `agui/mtti.py`, `agui/operator_telemetry.py`, `ui/.../MttiTimeline` | IMPLEMENTED |
| Operational Improvement Engine | `agui/improvement_engine.py` (returns NOT_MEASURED on empty) | IMPLEMENTED |
| EIC benchmark + gold dataset + IQS | `sentinel_core/eic/`, `eval/gold_standard/` | IMPLEMENTED |
| Synthetic Enterprise Validation corpus | `eval/enterprise/` (13 tool sources) | IMPLEMENTED |
| OIP #2–#5 (Incident Trends, Application Health, Service Reliability, Daily Brief) | `sentinel_core/oip/*` built + tested, **no API/UI consumer** | PARTIALLY IMPLEMENTED |
| Real multi-tool data (Splunk/Dynatrace/ServiceNow/K8s/…) | `workers/mcp_client.py` gateway path exists; **`GATEWAY_MODE=stub` default** | PARTIALLY IMPLEMENTED (config-gated) |
| Autonomous / agentic planner (Think→Act→Observe) | present; `AGENTIC_PLANNER` **default false** | PARTIALLY / EXPERIMENTAL (opt-in) |
| Shadow reasoning engines T1–T5 (causal/validation/decision) | `sentinel_core/*` reachable, **default OFF** | EXPERIMENTAL (opt-in) |
| Estate-wide accessibility (beyond investigation nav) | investigation nav done (H-3); other pages not audited | PARTIALLY IMPLEMENTED |
| MTTI reduction / operator acceleration | improvement-report → `NOT_MEASURED`; no pilot | NOT YET VALIDATED |
| Operator trust / adoption | no operator telemetry recorded | NOT YET VALIDATED |
| RCA correctness at scale | gold IQS 0.818 @ **n=3, underpowered** | NOT YET VALIDATED |
| Enterprise-scale performance / load | no load evidence in repo | NOT YET VALIDATED |
| Wave 3 runtime retrieval authority | present, **OFF** | FUTURE WORK |
| Autonomous remediation | HITL only; no auto-apply path enabled | FUTURE WORK |

## Architecture Inventory
- **Runtime:** intake → `investigate()` (thread pool) → `IncidentState` in
  `agui/state_store.py` → WebSocket → SPA. Deterministic core; fail-open phases
  with fail-closed G1–G5 gates.
- **Operator-intelligence seam:** `agui/oip_adapter.py` maps completed
  investigations → `operational_health` (the one wired OIP service).
- **Measurement:** `/api/v1/…/mtti`, `/operator-mtti`, `/operator-events`,
  `/improvement-report` — all mounted (verified via OpenAPI in prior cycles).
- **Data source:** `workers/mcp_client.py` — real gateway path OR stub fixtures;
  default stub.

## Documentation Inventory
- **README.md** (~50 KB) — headline corrected to "decision support" in an
  earlier commit, but the **body still describes the agentic loop as if
  default** → needs alignment (see Unsupported Claims).
- **docs/**: `certification/` (24+ reports incl. this), `ovp/`, `pilot/`,
  `ux/` (Phase 2 audit + closure), `oip/`, `enterprise/`, `eic/`, `ode/`.
- Some older docs describe intended/roadmap capability (e.g. full 5-surface OIP)
  that is only partially wired — README must not inherit those claims.

## Claim Validation Matrix (for README)
| Candidate README claim | Classification |
|---|---|
| "Deterministic, replayable RCA with byte-identical investigations" | IMPLEMENTED |
| "Every conclusion is evidence- and confidence-attributed; nothing fabricated" | IMPLEMENTED |
| "React investigation workspace: summary, timeline, evidence, topology, replay, MTTI" | IMPLEMENTED |
| "Operational Health rollup from completed investigations" | IMPLEMENTED |
| "System + operator MTTI instrumentation and an ROI improvement engine" | IMPLEMENTED |
| "Engine-agnostic EIC benchmark + enterprise validation corpus" | IMPLEMENTED |
| "Five operator-intelligence surfaces" | PARTIALLY IMPLEMENTED (1 wired, 4 built-not-wired) |
| "Investigates Splunk/Dynatrace/ServiceNow/… live" | PARTIALLY IMPLEMENTED (gateway config; stub default) |
| "Autonomous, closed-loop, no human in the loop" | FUTURE WORK (default off; HITL) |
| "Reduces MTTI / improves operator outcomes" | NOT YET VALIDATED |
| "Production-ready / enterprise-validated" | NOT YET VALIDATED |

## Strengths (implemented)
Determinism + hermetic replay; evidence/confidence provenance; a real BFF + SPA;
one operator surface wired end-to-end; complete offline measurement + validation
(MTTI, telemetry, improvement engine, EIC, enterprise corpus); secure auth
default; no fabricated outputs; 5,982 green tests.

## Known Limitations
Stub data by default; 4 of 5 OIP surfaces unwired; operator outcomes unproven;
RCA correctness underpowered (n=3); estate-wide a11y partial; repo sprawl (~62
feature flags, multiple compose/Dockerfiles).

## Technical Debt
~62 `*_ENABLED` flags (some overlapping); duplicate `docker-compose.yaml`/`.yml`;
4 Dockerfiles; large doc set with some roadmap-era claims; 4 orphaned OIP
services (built + tested, unconsumed).

## Unsupported Claims (must not appear as fact in README)
- "Autonomous / no human in the loop" as the default (it is opt-in, off).
- "Five operator surfaces available" (only Operational Health is reachable).
- "Investigates real data out of the box" (stub default).
- "Proven MTTI reduction / production-validated" (NOT YET VALIDATED).

## Recommended README Structure
1. What SentinelAI is (honest one-liner: deterministic, evidence-grounded RCA +
   decision support; HITL by default). 2. Architecture. 3. Investigation
   lifecycle. 4. Evidence model. 5. Confidence model. 6. Determinism & Replay.
   7. Operator Workspace (Phase 2). 8. Operational Intelligence (1 wired; note
   4 built-not-wired). 9. Measurement (system/operator MTTI, improvement engine).
   10. Validation (EIC/gold/enterprise corpus; outcomes NOT_YET_VALIDATED).
   11. Limitations. 12. Configuration (gateway mode, auth). 13. Development &
   Testing. 14. Deployment. 15. Pilot status. 16. Contribution. Each capability
   line tagged with its classification.

## README Writing Risks
Inheriting roadmap-era autonomy language; presenting partial OIP as complete;
implying real-data-by-default; implying validated outcomes. Mitigation: gate
every claim on the matrix above; label NOT_YET_VALIDATED explicitly.

## Readiness Decision

**READY FOR README.** The product is inventoried, every candidate claim is
classified against the repository, and the risks + honest structure are defined.
The README can now be written as an accurate technical specification of what
exists today.
