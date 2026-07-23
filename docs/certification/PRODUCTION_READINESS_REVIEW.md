# SentinelAI — Production Readiness Review (Supervised OCC Pilot)

**Release gate.** Evaluation only — no functionality added, no architecture
changed. Every finding is backed by observed code, config, tests, or deployment
artifacts (paths cited). Question answered: *can SentinelAI safely support a
supervised OCC production pilot?*

---

# EXECUTIVE SUMMARY

- **Mission Status: COMPLETE** (review executed against the current tree).
- **Overall Verdict: GO WITH LIMITATIONS** — safe for a *supervised, read-only,
  shadow* pilot once one configuration prerequisite (real data gateway) is met.
- **Production Readiness Score: 72 / 100** (for a supervised pilot; not full
  unattended production).

The engine's integrity is production-grade (determinism, replay, evidence and
confidence provenance, 5971 green tests). Security defaults are now correct
(auth ON, no baked secret). The trust-breaker (fabricated PR) is removed. The
one thing that makes a pilot *meaningful* — pointing at real data instead of the
default stubs — is a config action, not an engineering gap. No **safety**
blocker prevents a supervised shadow pilot.

---

# SYSTEM INVENTORY

| Component | Evidence | Status |
|---|---|---|
| Investigation engine | `supervisor/` (~41.7k LOC), `investigate()` | Wired |
| BFF API (FastAPI) | `agui/main.py` — 18 routers, OpenAPI enumerates the full route set | Wired |
| Operator surfaces (OIP) | `/api/v1/operational-health` **wired**; Incident Trends / Application Health / Service Reliability / Daily Brief **built but NOT wired** | Partial |
| MTTI + operator telemetry | `/api/v1/investigations/{id}/mtti`, `/operator-mtti`, `/operator-events`, `/improvement-report` all mounted | Wired |
| UI | React SPA; `ui/dist` committed + served by `agui/main.py`; Dockerfile.ui (nginx) | Wired |
| Storage | `agui/state_store.py` — InMemory + DynamoDB backends; receipt store (local/S3) | Wired |
| Auth | `agui/middleware/auth.py` — JWT (HS256/Cognito) + RBAC + honeypot | Wired |
| Deployment | `Dockerfile.bff` (copies `sentinel_core/`), `docker-compose.agui.yaml`, health probes | Wired |
| **Data source** | `.env.example:49` `GATEWAY_MODE=stub` — **synthetic fixtures by default** | **Config gap** |

Integration verified: OIP endpoint composes real completed investigations
(convergence commit); routes enumerate via `app.openapi()`.

---

# OPERATIONAL READINESS

- **Investigation workflow:** intake webhooks (Moogsoft/PagerDuty/ServiceNow/
  Grafana/CloudWatch) → `investigate()` → state store → WebSocket → SPA. Real,
  end to end.
- **Operator workflow:** open investigation → timeline/graph/evidence → risk/
  confidence → Operational Health (worst-first, drill-down) → MTTI panel. The
  wired path answers what/why/evidence/confidence; owner + next-action are
  strongest in Operational Health.
- **Failure handling:** phases are fail-open with fail-closed safety gates
  (`SYSTEM_REALITY.md`); startup degrades gracefully ("Ops persistence startup
  failed (non-fatal)", `agui/main.py:314`); graceful shutdown handler present.
- **Documentation:** operator runbook, deployment checklist, go/no-go,
  convergence, MTTI, operator-workflow, improvement-engine docs all present.
- **Supportability caveat:** with stub data the operator sees empty evidence —
  unsupportable for a real pilot; the gateway must be configured.

---

# RELIABILITY REVIEW

| Area | Evidence | Assessment |
|---|---|---|
| Runtime robustness | fail-open phases; workers fall back rather than crash | Good |
| Error handling | investigation failure → `INVESTIGATION_FAILED` event + FAILED state (`agui/api/investigations.py`) | Good |
| Timeouts | `time.monotonic` deadline guards in phases; pipeline `elapsed_ms` | Present |
| Resource cleanup | WS disconnect on unmount; shutdown handler | Present |
| Retry / degradation | stub fallback when a tool gateway is absent; non-fatal startup | Present |
| Startup/shutdown | `@app.on_event` startup + shutdown | Present |
| Config validation | auth guard raises at import if required-without-secret | Present, minimal |

Determinism/replay proven by the full regression, not just claimed.

---

# SECURITY REVIEW

| Control | Evidence | Status |
|---|---|---|
| Authentication | JWT HS256 + Cognito JWKS; **default `AGUI_AUTH_REQUIRED=true`** (`auth.py:39`) with import-time guard requiring a secret | PASS |
| Secrets | compose supplies `AGUI_JWT_SECRET` from env, **no baked dev secret** (`docker-compose.agui.yaml:19` uses `:?`) | PASS |
| Authorization | `viewer/operator/approver/admin` hierarchy + `require_role()` | PASS |
| Config safety | secure-by-default; stub mode is explicit (`GATEWAY_MODE`) | PASS |
| API exposure | honeypot gate fronts routes serving decoys to unauthenticated callers — **access-control-by-obscurity, not a substitute for RBAC** | PARTIAL |
| Trust integrity | fabricated GitHub PR removed; stub returns `github_gateway_not_configured` (`workers/mcp_client.py`) | PASS |

Security posture is materially improved and adequate for a supervised pilot on
a controlled network. The honeypot should not be relied on as the primary
authz boundary — enable JWT/RBAC (the default) for the pilot.

---

# DEPLOYMENT REVIEW

| Item | Evidence | Status |
|---|---|---|
| Build | `Dockerfile.bff` (BFF), `Dockerfile.ui` (nginx SPA); `ui/dist` committed | PASS |
| Packaging | `Dockerfile.bff` now copies `sentinel_core/` (was missing) | PASS |
| Rollback | produce-only OIP/telemetry surfaces imported by no runtime action path → removable without breaking the engine | PASS |
| Health checks | `/api/v1/health` (liveness), `/api/v1/health/tools` (integration status), `/ping`+`/ready` (agentcore) | PASS |
| Logging | structured `logging` across BFF + workers; investigation events recorded | PASS |
| Env config | `.env.example` / `.env.template`; compose secure defaults | PASS (set gateway) |

---

# RISK REGISTER

**CRITICAL**
- **Stub data by default.** Evidence: `.env.example:49` `GATEWAY_MODE=stub`,
  `AGENTCORE_GATEWAY_URL=""` default. Impact: an unconfigured pilot investigates
  empty synthetic fixtures → no real findings, no measurable MTTI. Mitigation:
  set `AGENTCORE_GATEWAY_URL` + `GATEWAY_MODE=live` before the pilot. This is a
  **config prerequisite**, not a code fix.

**HIGH**
- **4 of 5 OIP surfaces still orphaned** (Incident Trends, Application Health,
  Service Reliability, Daily Brief). Evidence: no BFF route/UI consumer.
  Impact: advertised operator value is only partly reachable. Mitigation: pilot
  on the wired Operational Health surface; wire or descope the rest post-pilot.
- **No operator-outcome evidence.** Evidence: empty operator-events log →
  `improvement-report` returns `NOT_MEASURED`. Impact: MTTI reduction is
  unproven. Mitigation: the pilot exists to produce it.

**MEDIUM**
- **UI accessibility gaps** (1 aria attribute, no keyboard/focus; desktop-only).
  Evidence: prior audit. Impact: fails procurement a11y bars; usable in a
  controlled desktop pilot. Mitigation: post-pilot.
- **Correctness underpowered** (gold n=3). Impact: RCA precision not
  statistically established. Mitigation: powered corpus via pilot.

**LOW**
- Repo sprawl / duplicate flags / dead panels. Evidence: prior audit. Impact:
  maintainability, not pilot safety. Mitigation: post-pilot cleanup.

---

# PILOT READINESS CHECKLIST

| Item | Status |
|---|---|
| Investigation engine deterministic + replayable | PASS |
| Evidence + confidence provenance intact | PASS |
| Auth enabled by default, no baked secret | PASS |
| No fabricated operational outputs | PASS |
| Health checks + graceful startup/shutdown | PASS |
| Rollback safe (produce-only surfaces) | PASS |
| One operator surface wired end-to-end (Operational Health) | PASS |
| MTTI + operator telemetry capture wired | PASS |
| Improvement engine returns NOT_MEASURED honestly on empty data | PASS |
| **Real data gateway configured** | **FAIL (must set before pilot)** |
| All 5 OIP surfaces reachable | PARTIAL (1/5) |
| UI accessibility / mobile | PARTIAL |
| Operator-outcome evidence (MTTI reduction) | PARTIAL (NOT_MEASURED — pilot produces it) |

---

# VALIDATION

| Guarantee | Result |
|---|---|
| Regression | **5971 passed, 2 skipped, 0 failed** (last full run) |
| Determinism | Preserved (byte-identical recompute + regression) |
| Replay | Preserved (no runtime path touched by recent work) |
| Evidence integrity | Preserved (R2 lifecycle intact) |
| Telemetry integrity | Preserved (additive; operator telemetry reuses pilot_telemetry) |
| Runtime | Unchanged (all recent work additive: new BFF routes, produce-only modules, UI panels) |
| UI | Typechecks + builds; `ui/dist` rebuilt |

---

# FINAL VERDICT

> **GO WITH LIMITATIONS.**

The platform can safely support a **supervised, read-only, shadow** OCC pilot on
a controlled network with authentication enabled — its integrity and safety
properties are proven and the trust/security defects are fixed. It is **not**
approved for unattended production: stub-by-default data, four unwired operator
surfaces, a11y gaps, and unproven operator outcomes are real limitations. None
of these is a *safety* blocker for a supervised pilot; the stub-data default is
the one item that must be configured for the pilot to produce meaningful data.

---

# RECOMMENDED NEXT STEP (exactly one)

**Configure the real data gateway (`AGENTCORE_GATEWAY_URL` + `GATEWAY_MODE=live`)
and launch the supervised pilot on the Operational Health surface.** This is the
single highest-value action: it converts every `NOT_MEASURED` result — MTTI,
operator acceleration, improvement backlog — into real evidence, which is the
only thing still missing.

---

# HONEST BOTTOM LINE

- **Production-ready:** deterministic investigation engine, hermetic replay,
  evidence/confidence provenance, secure auth defaults, no fabricated outputs,
  health checks, graceful startup/shutdown, safe rollback, one operator surface
  (Operational Health) wired end-to-end, and telemetry capture (MTTI, operator
  timeline, improvement engine) — all green at 5971 tests.
- **Not production-ready:** unattended operation (stub data by default), the
  other four OIP surfaces (unwired), UI accessibility/mobile, and any claim that
  MTTI is actually reduced (unproven — `NOT_MEASURED`).
- **Must complete before a supervised pilot:** configure the real data gateway;
  confirm auth enabled with a real secret; scope the pilot to the wired
  Operational Health surface.
- **Can wait until after pilot feedback:** wiring or descoping the remaining OIP
  surfaces, UI accessibility, corpus powering to n≥30, and repo/flag cleanup.

No finding above is speculative; each cites the code, config, or test that
supports it.
