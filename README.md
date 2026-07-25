# SentinelAI

SentinelAI is a **deterministic, evidence-grounded root-cause-analysis (RCA)
platform** for enterprise incident investigation. Given an incident, it runs a
reproducible five-phase investigation, attributes every conclusion to the
evidence and confidence behind it, and presents the result in an operator
workspace where a human reviews and decides. It is built for SRE / OCC teams who
need investigations that are **auditable, replayable, and honest** — including
honest about what it does *not* know. In its default configuration it is
**decision support, not automation**: the agentic planner and any runtime
remediation authority are off, and a human is always in the loop.

> This README describes the platform **as implemented today**. Every capability
> below is classified. The authoritative inventory is
> [`docs/certification/PRODUCT_READINESS_AUDIT.md`](docs/certification/PRODUCT_READINESS_AUDIT.md);
> where this document and the repository disagree, the repository wins.

**Status key:** ✅ IMPLEMENTED · 🟡 PARTIALLY IMPLEMENTED · ⚪ NOT YET VALIDATED · 🔵 FUTURE WORK

---

## Why SentinelAI Exists

Enterprise incident response is dominated by two costs: **time to identify** the
fault, and **trust** in the conclusion. Most tooling surfaces telemetry and
leaves correlation and judgement to the operator, under pressure, at 2 AM.
Two recurring failure modes follow:

- **Irreproducibility.** The same incident investigated twice yields different
  narratives, so conclusions cannot be audited or learned from.
- **Unattributed conclusions.** A root cause is asserted without a traceable
  chain to the evidence, so operators cannot calibrate their trust.

SentinelAI takes the position that an investigation is only useful if it is
**deterministic** (same inputs → byte-identical result), **evidence-grounded**
(every conclusion cites its evidence, and missing evidence is stated rather than
hidden), and **verifiable** (the whole investigation can be replayed from a
frozen snapshot). These properties are implemented and test-covered; whether
they *reduce operator MTTI in production* is a separate, not-yet-validated
question (see [Validation Status](#validation-status)).

---

## Core Design Principles

Only principles reflected in the current implementation are listed.

- **Deterministic investigations** — the engine reads no wall-clock, randomness,
  or uuid on its deterministic path; the same incident + pinned corpus produces
  a byte-identical result. ✅
- **Evidence over speculation** — evidence has an explicit lifecycle
  (`used` / `filtered` / `unavailable` / `error`); nothing is silently dropped
  and nothing is fabricated. ✅
- **Confidence provenance** — each confidence contribution is counted once and
  is reconstructible. ✅
- **Replayability** — an investigation is captured against a frozen corpus and
  can be re-streamed deterministically. ✅
- **Operator-first workspace** — the five decision questions (what / why /
  evidence / confidence / next) are answerable without navigating panels. ✅
- **Evaluation-driven development** — an engine-agnostic benchmark and a
  synthetic enterprise corpus grade investigations offline. ✅
- **Honest measurement** — where data is insufficient, the platform returns
  `NOT_MEASURED` instead of a fabricated number. ✅

---

## Architecture Overview

```
                       ┌──────────────────────────────┐
   Alert intake ─────► │  agui BFF (FastAPI)           │
   (Moogsoft, PD,      │  REST + WebSocket streaming    │ ◄──── React SPA (ui/)
    ServiceNow,        │  auth (JWT/RBAC)               │       Investigation
    Grafana,           └───────────────┬────────────────┘        Workspace
    CloudWatch)                        │
                                       ▼
                       ┌──────────────────────────────┐
                       │  Investigation engine          │
                       │  supervisor/  investigate()    │
                       │  Fetch → Classify → Collect →  │
                       │  Analyze → Persist  (G1–G5)     │
                       └───────┬──────────────┬─────────┘
                               │              │
                 evidence via  │              │  frozen corpus + hermetic replay
                 workers/ +    ▼              ▼  (supervisor/frozen_corpus.py,
                 MCP gateway   Evidence/      Replay          replay.py)
                 (stub by      Confidence
                  default)     provenance
                               │
                               ▼
                       Knowledge graph · State store (in-memory / DynamoDB)
                               │
                               ▼
                       Operator Intelligence (OIP) + Measurement
                       Operational Health · MTTI · Improvement engine
                               │
                               ▼
                       Evaluation: EIC benchmark · gold dataset ·
                       synthetic enterprise corpus
```

The deterministic core (`supervisor/`) is fail-open per phase with fail-closed
safety gates (G1–G5). The BFF (`agui/`) is a thin, produce-only layer over it;
operator-intelligence and measurement modules read completed investigations and
never mutate the engine.

---

## Investigation Lifecycle

The implemented flow (`supervisor/agent.py::investigate`, `supervisor/phases/`):

| Stage | Where | What happens |
|---|---|---|
| **Alert / Context** | `phases/fetch.py`, `phases/classify.py` | incident metadata fetched and classified; investigation features derive from the incident's own timestamp (no wall-clock). |
| **Evidence** | `phases/collect.py`, `workers/` | multi-source evidence collected via workers/MCP; lifecycle recorded (`_evidence_lifecycle`). |
| **Hypotheses / Root cause** | `phases/analyze.py`, `supervisor/helpers/` | deterministic correlation + scoring select a root cause; confidence provenance attached. |
| **Recommendations** | analyze + control | next action surfaced; remediation requires human approval (HITL). |
| **Replay** | `supervisor/replay.py`, `frozen_corpus.py` | investigation captured against a frozen corpus; deterministically re-streamable. |
| **Evaluation** | `sentinel_core/eic/`, `eval/` | graded offline against ground truth. |

---

## Key Capabilities

Populated from the Product Readiness Audit.

| Capability | Description | Status | Validation |
|---|---|---|---|
| Deterministic investigation engine | 5-phase pipeline, byte-identical results | ✅ IMPLEMENTED | full test suite; recompute equality |
| Frozen Corpus + Hermetic Replay | snapshot-per-run; replay reads only recorded corpus | ✅ IMPLEMENTED | `tests/frozen_corpus/`, replay tests |
| Evidence provenance + lifecycle | terminal states; no silent loss | ✅ IMPLEMENTED | `tests/confidence/test_evidence_lifecycle` |
| Confidence provenance | each contribution counted once | ✅ IMPLEMENTED | `tests/confidence/` |
| Knowledge graph | service topology / blast radius | ✅ IMPLEMENTED | `tests/` graph suites |
| BFF API + WebSocket + React SPA | live investigation workspace | ✅ IMPLEMENTED | `tests/agui/`, UI build |
| Investigation Workspace (Phase 2) | summary header, keyboard tablist, wide-display layout | ✅ IMPLEMENTED | `docs/ux/PHASE2_CLOSURE_REPORT.md` |
| Alert intake webhooks | Moogsoft/PagerDuty/ServiceNow/Opsgenie/Grafana/CloudWatch | ✅ IMPLEMENTED | `agui/api/intake.py` |
| Authentication (JWT/RBAC) | secure-by-default; import-time secret guard | ✅ IMPLEMENTED | `agui/middleware/auth.py` |
| Operational Health (OIP #1) | per-service health from completed investigations, wired end-to-end | ✅ IMPLEMENTED | `tests/oip/`, `/api/v1/operational-health` |
| MTTI instrumentation | system + operator timelines | ✅ IMPLEMENTED | `tests/agui/test_mtti`, `test_operator_telemetry` |
| Operational Improvement Engine | telemetry → ranked backlog (NOT_MEASURED on empty) | ✅ IMPLEMENTED | `tests/agui/test_improvement_engine` |
| EIC benchmark + gold dataset + enterprise corpus | engine-agnostic scoring; 13 tool sources | ✅ IMPLEMENTED | `tests/eic/`, `tests/enterprise/` |
| OIP surfaces #2–#5 | Incident Trends, Application Health, Service Reliability, Daily Brief — built + tested, **not wired to API/UI** | 🟡 PARTIALLY | `tests/oip/`; no consumer |
| Live multi-tool data | real gateway path exists; **`GATEWAY_MODE=stub` by default** | 🟡 PARTIALLY | `workers/mcp_client.py` |
| Agentic planner / shadow reasoning engines | present, **default off** | 🟡 EXPERIMENTAL | opt-in flags |
| Estate-wide accessibility | investigation navigation done; other pages not audited | 🟡 PARTIALLY | `docs/ux/` |
| MTTI reduction / operator acceleration | requires a pilot | ⚪ NOT YET VALIDATED | improvement-report → NOT_MEASURED |
| Operator trust / adoption | no operator sessions recorded | ⚪ NOT YET VALIDATED | — |
| RCA correctness at scale | gold IQS 0.818 @ **n=3, underpowered** | ⚪ NOT YET VALIDATED | `eval/gold_standard/evaluation.json` |
| Enterprise-scale performance | no load evidence | ⚪ NOT YET VALIDATED | — |
| Wave 3 runtime retrieval authority | present, off | 🔵 FUTURE WORK | — |
| Autonomous remediation | HITL only today | 🔵 FUTURE WORK | — |

---

## Repository Structure

| Path | Purpose |
|---|---|
| `supervisor/` | investigation engine — `investigate()`, the 5 phases, planner, receipts |
| `sentinel_core/` | core models + subsystems — `oip/` (operator intelligence), `eic/` (benchmark), `frozen_corpus`, `models/`, `investigation_value/` |
| `agui/` | Backend-for-Frontend (FastAPI): REST, WebSocket, auth, state store, MTTI/operator/improvement APIs |
| `ui/` | React + Vite + Tailwind investigation workspace SPA (built bundle in `ui/dist`) |
| `workers/` | evidence collectors + MCP client (real gateway or stub) |
| `integrations/`, `intelligence/`, `knowledge/` | tool integrations, live intelligence subsystems, knowledge stores |
| `eval/` | evaluation corpora + harnesses — `gold_standard/`, `eic/`, `enterprise/`, `ovp/` |
| `tests/` | ~5,984 tests (pytest) |
| `docs/` | architecture, certification, UX, evaluation, pilot, governance docs |
| `config/`, `database/`, `scripts/` | playbooks, persistence, tooling |

---

## Documentation Guide

Deep detail lives under `docs/` (only directories that exist are linked):

| Area | Location |
|---|---|
| Architecture | [`docs/architecture/`](docs/architecture/) |
| Certification & readiness | [`docs/certification/`](docs/certification/) — incl. [`PRODUCT_READINESS_AUDIT.md`](docs/certification/PRODUCT_READINESS_AUDIT.md), `RELEASE_CERTIFICATION_V1.0.md`, `PRODUCTION_READINESS_REVIEW.md` |
| Operator workspace / UX | [`docs/ux/`](docs/ux/) — Phase 2 audit + [`PHASE2_CLOSURE_REPORT.md`](docs/ux/PHASE2_CLOSURE_REPORT.md) |
| Operator Intelligence (OIP) | [`docs/oip/`](docs/oip/) |
| Evaluation | [`docs/eic/`](docs/eic/), [`eval/enterprise/README.md`](eval/enterprise/README.md) |
| Validation program & pilot | [`docs/ovp/`](docs/ovp/), [`docs/pilot/`](docs/pilot/) |
| Effectiveness / discovery (research) | [`docs/effectiveness/`](docs/effectiveness/), [`docs/ode/`](docs/ode/), [`docs/shadow_pilot/`](docs/shadow_pilot/) |
| Engineering principles | [`CLAUDE.md`](CLAUDE.md) |

---

## Getting Started

> Only supported workflows are documented.

**Prerequisites:** Python 3.11, Node 22 (for the UI).

```bash
# 1. Install
pip install -r requirements.txt

# 2. Run the test suite (pytest; testpaths=tests in pyproject.toml)
pytest -q

# 3. Configure (secure + explicit defaults)
export AGUI_AUTH_REQUIRED=true          # auth on by default; set a real secret:
export AGUI_JWT_SECRET="<your-secret>"   # the BFF refuses to start without one
export GATEWAY_MODE=stub                 # 'stub' = synthetic fixtures (default);
# export AGENTCORE_GATEWAY_URL=...       # set this + GATEWAY_MODE=live for real data
```

**Run the BFF (API + serves the built SPA):**
```bash
python -m uvicorn agui.main:app --host 0.0.0.0 --port 8081
```

**Build / develop the UI:**
```bash
cd ui && npm install && npm run build     # produces ui/dist (served by the BFF)
# or: npm run dev                          # Vite dev server
```

**Run an evaluation against the synthetic enterprise corpus:**
```bash
python eval/enterprise/build_corpus.py    # (re)materialize eval/enterprise/corpus.json
python -c "from eval.enterprise.validate import validate; print(validate())"
# -> NOT_MEASURED without engine submissions (the corpus is the answer key)
```

**Replay** is exercised through the BFF replay API
(`/api/v1/investigations/{id}/replay`) and the deterministic replay engine
(`supervisor/replay.py`).

> **Important:** with `GATEWAY_MODE=stub` (the default) investigations run against
> synthetic fixtures. Configure a real `AGENTCORE_GATEWAY_URL` before using
> SentinelAI on real incidents.

---

## Validation Status

Exactly matches the Product Readiness Audit.

- **Implemented (✅):** deterministic engine, frozen corpus + hermetic replay,
  evidence + confidence provenance, knowledge graph, BFF + WebSocket + SPA,
  Phase-2 workspace, alert intake, secure auth, Operational Health (wired),
  system + operator MTTI, improvement engine, EIC + gold + enterprise corpus.
  Last full test run: **5,982 passed / 2 skipped / 0 failed**.
- **Partially implemented (🟡):** OIP surfaces #2–#5 (built + tested, not wired);
  live multi-tool data (gateway config; stub by default); agentic planner /
  shadow engines (opt-in, off); estate-wide accessibility.
- **Not yet validated (⚪):** operator MTTI reduction, operator trust/adoption,
  RCA correctness at scale (gold n=3, underpowered), enterprise-scale
  performance. These require a supervised pilot with real operators and a
  powered corpus (`docs/pilot/`, `docs/ovp/`).
- **Future work (🔵):** Wave 3 runtime authority, autonomous remediation.

---

## Current Limitations

Only limitations supported by the audit:

- **Operator outcomes are not measured.** No pilot has run; MTTI reduction,
  trust, and adoption are `NOT_MEASURED`.
- **Real integrations depend on deployment configuration.** The default is stub
  data; a real MCP gateway must be configured for live investigations.
- **Only one operator-intelligence surface is wired** (Operational Health). The
  other four OIP services are built and tested but have no API/UI consumer.
- **RCA correctness is underpowered** — the gold dataset is `n=3`.
- **Accessibility is complete for investigation navigation only**; other pages
  are not yet audited.
- **Not a hands-off autonomous system** in its default configuration — it is
  human-in-the-loop decision support.

---

## Roadmap

Every item here is classified **FUTURE WORK** or **PARTIALLY IMPLEMENTED** —
nothing on this list is claimed as implemented.

- **Near-term:** run the supervised OCC pilot to convert `NOT_MEASURED` outcomes
  into evidence; configure real data gateways; close estate-wide accessibility.
- **Mid-term:** wire (or descope) OIP surfaces #2–#5 based on observed pilot
  demand; power the evaluation corpus toward `n ≥ 30`; reduce feature-flag /
  packaging sprawl.
- **Long-term:** promote opt-in reasoning engines (agentic planner, shadow
  Tranches) only where pilot evidence justifies; evaluate Wave-3 runtime
  authority and any autonomous-remediation path under strict safety gating.

Roadmap priorities are intended to be driven by pilot evidence, not by feature
availability.

---

## Contributing

Engineering expectations (see [`CLAUDE.md`](CLAUDE.md)):

- **Evidence-first** — every claim must trace to code, tests, or measured data;
  where data is missing, return `NOT_MEASURED`, never a fabricated value.
- **Deterministic** — no wall-clock / randomness on the investigation path;
  canonical JSON, content-addressed ids.
- **Regression-safe** — the full suite must pass (currently 5,982); changes are
  additive and backward compatible by default.
- **Evaluation-backed** — non-trivial changes should be gradeable against the
  EIC / enterprise corpus.
- **Replay-safe** — do not break the frozen-corpus / hermetic-replay contract.

---

## License

No `LICENSE` file is present in the repository at this time; licensing is
determined by the repository owner. Do not assume an open-source grant.
