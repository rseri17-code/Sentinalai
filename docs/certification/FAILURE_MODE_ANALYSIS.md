# FAILURE_MODE_ANALYSIS.md
**SentinelAI — Production Readiness Certification · Phase 3**
Dependency-failure behaviour + determinism-risk audit of the live path. All findings
cited; the two determinism defects were independently re-verified against source.

## Architecture
Every external evidence source is reached through **one chokepoint** —
`workers/mcp_client.py::McpGateway.invoke()` — wrapped by
`supervisor/agent.py::_call_worker` (timeout + 1 retry + circuit breaker). Thin workers
(Splunk=`log_worker`, Dynatrace/SignalFx=`apm_worker`, Sysdig/Moogsoft=`metrics/ops_worker`,
ServiceNow/CMDB=`itsm_worker`) delegate to the gateway.

## Section A — Dependency failure handling
**Crash-safety: STRONG.** `_call_worker` (agent.py:1226-1341) guarantees a dict return for
every source: circuit-breaker precheck, budget gate, per-call `future.result(timeout)`,
1 retry with exponential backoff, and on total failure returns `{"error": ...}` — logged
and recorded to receipts. The gateway degradation ladder (policy gate → rate-limit →
gateway → legacy ARN → stub) never raises. **No dependency has zero handling.**

| Dependency | Unavailable behaviour | Verdict |
|---|---|---|
| Splunk | allowlist reject / empty `{"logs":{"results":[],"count":0}}` | graceful |
| Dynatrace/SignalFx | gateway never raises; SignalFx enrich try/except warn | graceful |
| Moogsoft/Sysdig | empty stubs `{}` / `{"problems":[]}` | graceful |
| CMDB | worker-missing → None; traversal try/except → None | graceful |
| Incident fetch | empty → None → `investigate` early-returns `_empty_result` | graceful |
| Empty evidence | Evidence Gate G1/G4 BLOCK → explicit "insufficient evidence", not a fabricated RCA | graceful |

**Observability gaps (MED — silent degradation):**
- `collect.py:218-221` experience-future failure → `[]` **with no log**.
- `collect.py:262-265` KG-future failure → `[]` **with no log** (historical at 207-211
  *does* warn — inconsistent).
- Trace/visual/tool-rec failures logged only at `debug` (invisible at INFO).
- **Optimistic-fallback (MED):** `mcp_client.py:975-1002` tool discovery returns
  `_ALL_KNOWN_SERVERS` on *any* discovery error → a down gateway is reported as "all tools
  connected"; the agent proceeds and fails per-call instead of knowing upfront.

Net: the system is **crash-safe but under-observable** — failures rarely abort an
investigation; they silently shrink the evidence set. For production, silent evidence
loss that lowers confidence is itself a trust risk (operator can't tell "clean, low
evidence" from "3 sources were down").

## Section B — Determinism risk (VERIFIED)

### BLOCKER-class defects (re-verified against source)
| # | Location | Defect | Impact |
|---|---|---|---|
| **D-1** | `agent.py:2094` `_get_change_time_window` | Computes `elapsed_hours = datetime.now(utc) − incident.created_at` → sets `params["time_window_hours"]` for `get_change_data`. The code comment says "anchor change queries to incident time window" — but the impl uses **wall-clock**. | **Replaying the same incident later queries a wider change window → different change evidence.** Breaks replay fidelity + the core "same input → same output" guarantee. **HIGH.** |
| **D-2** | `agent.py:3277` `_build_dna_evidence_dict` | `flat["incident_hour"] = datetime.now(utc).hour` feeds the Incident-DNA fingerprint → `PatternRegistry.match` → **primed hypotheses**. | Same incident investigated at a different hour → different fingerprint feature → potentially different primed candidates → potentially different ranked output. **HIGH.** |

**Exact fixes (surgical, for the hardening task — NOT applied during this validation pass):**
- D-1: derive the window from `incident.created_at` alone (a fixed look-back), never `now()`.
- D-2: derive the hour from the incident timestamp, not `now()`.

### Canonicalisation gaps (MED)
| # | Location | Defect |
|---|---|---|
| D-3 | `supervisor/replay.py:81` | stored replay artifact `json.dumps(..., default=str)` **without `sort_keys`** → two runs can produce byte-different artifacts for identical logical content. Replay is the determinism backbone; its artifact should be canonical. |
| D-4 | `supervisor/rca_report.py:79` | human-facing RCA report not canonicalised (no `sort_keys`). |
| D-5 | `agent.py:1986-2011` `_execute_playbook` | timing-based future cancellation on loop-escalation → the **set** of evidence keys collected can differ run-to-run (`PARALLEL_PLAYBOOK` default ON). `sort_keys` cannot fix a content difference. |

### Default-OFF violations (LOW — gated off, must stay off or be fixed first)
- D-6 `network_worker.py:74-79` set-iteration order into a list; D-7
  `thousandeyes/normalizer.py:65-66` `uuid4` + `now()` in evidence payload. Both behind
  `ENABLE_THOUSANDEYES_RCA` (default off). Must not be enabled in a deterministic
  deployment without fixing.

### SAFE (verified — no action)
Canonical/receipt/artifact serializers **do** sort (`investigation_artifact/serialization.py:26`,
`models/receipts.py:154`, `context_persister.py:161`); CMDB traversal uses hardcoded
`hours=24` + ordered deque; collect merges under fixed literal keys after explicit
`.result()`; `analyze.py:453` sorts evidence keys; MCP dedup signature uses
`sort_keys=True`. The episodic-memory `uuid4`/`now()` write (agent.py:1153,1172) is a
side-effect not in the returned result (low-sev store pollution only).

## Assessment
- **Failure resilience:** CONDITIONALLY CERTIFIED — crash-safe, but silent evidence loss
  and the optimistic tool-discovery fallback reduce operator trust; add failure
  observability (backlog F-obs).
- **Determinism of the live path:** **NOT CERTIFIED — 2 verified core defects (D-1, D-2)**
  plus 3 canonicalisation gaps (D-3–D-5). These are the top certification blockers. They
  are surgical to fix but must be fixed and regression-verified before any "byte-identical
  in production" or "exact replay" claim.
