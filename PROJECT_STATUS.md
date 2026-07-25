# SentinelAI — Project Status

A one-page snapshot of where the project stands. Unlike `README.md` (the stable
technical specification), this file changes each milestone. For the authoritative
capability inventory see
[`docs/certification/PRODUCT_READINESS_AUDIT.md`](docs/certification/PRODUCT_READINESS_AUDIT.md).

| Field | Value |
|---|---|
| **Current Milestone** | V1.0 — engineering + governance + documentation complete; pre-pilot |
| **Current Phase** | Pilot Readiness (transition from engineering to product validation) |
| **Latest Completed Milestone** | Phase 3 — Documentation (README rewritten as an accurate technical spec) |
| **Current Focus** | Pilot execution and evidence collection (converting `NOT_MEASURED` outcomes into measured ones) |
| **Next Major Milestone** | Supervised OCC pilot validation |
| **Last Updated Commit** | `9327332` (Phase 3 README) |

## Current Validation Status

- **✅ Implemented:** deterministic 5-phase investigation engine; frozen corpus +
  hermetic replay; evidence + confidence provenance; knowledge graph;
  BFF + WebSocket + React workspace; Phase-2 investigation workspace UX; alert
  intake; secure auth; Operational Health (OIP #1) wired end-to-end; system +
  operator MTTI instrumentation; improvement engine; EIC benchmark + gold +
  synthetic enterprise corpus. Tests: **5,982 passed / 2 skipped / 0 failed**.
- **🟡 Partially implemented:** OIP surfaces #2–#5 (built + tested, not wired);
  live multi-tool data (real gateway path exists; `GATEWAY_MODE=stub` default);
  agentic planner / shadow reasoning engines (opt-in, off); estate-wide
  accessibility (investigation navigation done).
- **⚪ Not yet validated:** operator MTTI reduction; operator trust / adoption;
  RCA correctness at scale (gold dataset `n=3`); enterprise-scale performance.
- **🔵 Future work:** Wave-3 runtime authority; autonomous remediation.

## Known Limitations (from the audit)

- Operator outcomes are not measured — no pilot has run.
- Real integrations require deployment configuration (stub data by default).
- Only one of five operator-intelligence surfaces is wired.
- RCA correctness is underpowered (`n=3`).
- Human-in-the-loop decision support by default — not a hands-off autonomous
  system.

## What Drives the Roadmap

Pilot evidence, not feature availability. The next engineering work should be
selected from what a supervised pilot measurably shows operators need — see
[`docs/pilot/`](docs/pilot/) and [`docs/ovp/`](docs/ovp/).

*Update this file at each milestone: refresh the phase, focus, validation
status, and Last Updated Commit.*
