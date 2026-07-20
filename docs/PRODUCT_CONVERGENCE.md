# SentinelAI — Product Convergence (Operational Health vertical slice)

Fixes the primary architectural defect from the audit: the Operational
Intelligence (OIP) layer existed with tests + docs but was **orphaned** — not
imported by `supervisor`, not exposed by `agui`, not consumed by the React UI,
not packaged into the deployed image. This change wires **one** OIP surface —
Operational Health — end to end, and removes the trust/production defects that
would break an operator's confidence. No new functionality; no engine redesign;
no OIP #6/#7.

## Phase 1 — True runtime (traced, not assumed)

```
Incident → agui/api/intake (webhooks) → supervisor.investigate() [thread pool]
        → result dict → agui state_store (IncidentState) → WebSocket (ws_manager)
        → React SPA (ui/, served from ui/dist by agui/main.py)
```

`IncidentState` is the runtime source of truth for a completed investigation
(`agui/state_store.py`, persisted via `put_state`). It carried only
`root_cause`/`confidence` from the result — the richer R1/R2 signals were
discarded.

## Phase 2 — OIP wiring matrix (before)

| Service | imports | REST | WS | UI | packaged | Status |
|---|---|---|---|---|---|---|
| Operational Health | tests/eval only | none | none | none | no (`Dockerfile.bff` skipped `sentinel_core/`) | **Orphaned** |
| Incident Trends | tests/eval only | none | none | none | no | Orphaned |
| Application Health | tests/eval only | none | none | none | no | Orphaned |
| Service Reliability | tests/eval only | none | none | none | no | Orphaned |
| Daily Operations Brief | tests/eval only | none | none | none | no | Orphaned |

## Phase 3 — Convergence design (smallest)

```
Investigation Engine → IncidentState (runtime) → oip_adapter → operational_health
                     → /api/v1/operational-health → typed client → React screen
                     → drill-down to /investigations/:id
```

The engine stays the source of truth. OIP is a **consumer** of investigation
results; the adapter only reshapes existing data. No investigation logic,
evidence, or reasoning is duplicated. Signals the default runtime does not emit
(validation/causal shadow engines, off by default) are **left absent** and
disclosed — never fabricated.

## Phase 4 — Implemented (Operational Health only)

| Layer | Change |
|---|---|
| Runtime capture | `IncidentState` gains additive `corpus_version` + `evidence_lifecycle`; `_dispatch_investigation` lifts them off the real result (backward compatible) |
| Adapter | `agui/oip_adapter.py` — completed states → (results, incidents, drilldown); honest `signal_coverage` |
| Endpoint | `GET /api/v1/operational-health` (`agui/api/operational_health.py`) composes `sentinel_core.oip.operational_health`; returns health + `drilldown` + `signal_coverage`; registered in `agui/main.py` |
| Typed API | `operationalHealthApi` + `OperationalHealthResponse` in `ui/src/api/client.ts` |
| Screen | `ui/src/components/OperationalHealth` — services worst-first, band/score/why/confidence/verifiable/evidence, drill-down link to the supporting investigation, loading/empty/error states, honest signal-coverage banner |
| Nav | route `/operational-health` (AppShell) + Sidebar link |

**Operator workflow, end to end:** see unhealthy services → select one → open
the supporting investigation → inspect evidence/receipts → see confidence →
decide next action — without leaving SentinelAI.

## Phase 5 — Trust breakers removed

- **Fabricated GitHub PR eliminated.** `workers/mcp_client.py::_stub_github` no
  longer returns a plausible mergeable PR (`#9001`, real-looking URL). In stub
  mode it returns an explicit non-actionable marker (`pr: null`, `stub: true`,
  `error: github_gateway_not_configured`). The system never fabricates a
  remediation. Guarded by `tests/test_stub_no_fabricated_pr.py`.

## Phase 6 — Production defaults

- `docker-compose.agui.yaml`: auth **ON** by default
  (`AGUI_AUTH_REQUIRED:-true`), **no baked secret** (compose fails fast unless
  `AGUI_JWT_SECRET` is supplied), explicit `GATEWAY_MODE`/`AGENTCORE_GATEWAY_URL`
  so stub mode is a conscious choice, not an accident.
- `Dockerfile.bff`: now copies `sentinel_core/` so the runtime (incl. the OIP
  endpoint) is actually present in the image.
- `ui/vite.config.ts`: source maps off — no multi-MB map shipped to production.

## Phase 7 — Product honesty

- README headline corrected: SentinelAI's **default** posture is deterministic,
  human-in-the-loop **decision support**, not "autonomous… without a human in
  the loop." Autonomy (`AGENTIC_PLANNER`, Wave 3) is opt-in and OFF by default.

## Phase 8 — Validation

- Investigation engine unchanged (capture is additive; no phase/replay/evidence/
  confidence logic touched).
- New Python tests: adapter + endpoint (`tests/agui/test_operational_health_endpoint.py`),
  trust fix (`tests/test_stub_no_fabricated_pr.py`).
- UI typechecks (`tsc --noEmit`) and builds (`vite build`); `ui/dist` rebuilt so
  the screen ships in the served SPA.
- Full regression: see the commit message for this change.

## Remaining (backlog — not done here, by scope)

The other four OIP services stay orphaned by design of this slice ("implement
ONLY ONE"). Each can follow the identical pattern (adapter reuse + endpoint +
screen) **or** be removed if a second surface is not wanted. That is a product
decision, deferred — not silently shipped.
