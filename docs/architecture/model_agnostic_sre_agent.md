# Model-Agnostic SRE Investigation Agent — Architecture Audit

**Status:** Slices 0–4 SHIPPED on `main` (#76–#80). Slice 5 (OpenAI Chat Completions `InferencePort`) is this PR.  
**Date:** 2026-09-21 (audit at `25579b6` / `0d625ec`; implementation through Slice 5)  
**Source of truth:** this repository on `main` after #80, plus this PR for OpenAI.  
**Method:** OBSERVE → REASON → ACT → VERIFY → LEARN. Original audit claims cited source at commit `0d625ec`. Implementation status in the banner and §1.11 / §2 / §5 / §8 / §9 **supersedes** audit-time “Bedrock-only / GATEWAY_MODE unread” statements.

**Shipped on main**
| Slice | PR | What |
|---|---|---|
| 0 | #76 | Docs/config honesty (this audit) |
| 1 | #77 | `converse()` factory over `InferencePort`; `LLM_ENABLED` default false |
| 2 | #78 | Anthropic Messages adapter; acceptance A–D |
| 3 | #79 | `GATEWAY_MODE` honored; single `.env.example`; health reports real port |
| 4 | #80 | `MCP_GATEWAY_URL` alias; YAML playbook worker aliases |
| 5 | this PR | OpenAI Chat Completions adapter behind the same port |

**Remaining (not this PR)**
- LICENSE / OSS grant — **owner deciding separately**
- AgentCore Memory portable replacement — **optional overlay**; JSON KG / experience store remain the clone path
- Token streaming, native tool-use, routing dev-loop Anthropic clients through `InferencePort`
- Whole-repo ruff / mypy / bandit cleanup — **UNKNOWN / pre-existing on `main`**; do not treat as Slice 5 debt
- Equal live-provider RCA quality on production incidents — **UNKNOWN** (gold n=3)

This document answers five questions:

1. What Sentinal is today
2. What prevents plug-and-play / model-agnostic use
3. Target model-agnostic architecture
4. Minimum changes required
5. How we prove it works

Companion: [`model_coupling_table.md`](model_coupling_table.md) enumerates every observed coupling.

Related existing docs (not replaced): [`README.md`](../../README.md), [`docs/architecture/deterministic_planner.md`](deterministic_planner.md), [`docs/architecture/incident_intelligence_memory.md`](incident_intelligence_memory.md), [`docs/certification/PRODUCT_READINESS_AUDIT.md`](../certification/PRODUCT_READINESS_AUDIT.md). Root [`ARCHITECTURE_AUDIT.md`](../../ARCHITECTURE_AUDIT.md) (dated 2026-03-07/08) is **stale** relative to the live five-phase pipeline in `supervisor/agent.py::investigate`.

---

## Scope and non-goals

**In scope:** evidence-backed current-state architecture; coupling map; target model-agnostic contract; gap analysis; smallest provider boundary; prioritized migration plan; acceptance tests.

**Out of scope for the original audit PR and still out of scope for Slice 5:** refactoring `supervisor/agent.py` SRE logic; changing production prompts; changing MCP integrations; enabling live writebacks; relicensing the repo; token streaming; native LLM tool-use; AgentCore Memory replacement.

Adapters (Null / Bedrock / Anthropic / OpenAI) are **implemented** behind `converse()` as of Slices 1–5. The original audit did not implement them.

---

## 1. What Sentinal is today

SentinalAI (package name `sentinalai` in `pyproject.toml`; README title “SentinelAI”) is a **deterministic, evidence-grounded RCA investigation engine** with an **optional LLM overlay**. In the default CI and eval configuration (`LLM_ENABLED=false` in `.github/workflows/ci.yml` and `.github/workflows/pr.yml`), investigations do not call a model. The engine classifies an incident, runs a typed playbook of MCP tool workers, scores hypotheses from evidence, and returns a structured RCA result.

It is **not**, in default configuration, an autonomous remediator. `ITSM_WRITEBACK_ENABLED` defaults false (`intelligence/itsm_writebacks.py`, `supervisor/sentinel_config.py`). Remediation is generated as guidance; HITL approval is the documented product posture (`README.md`).

It is **not** currently licensed for third-party cloning. `pyproject.toml` sets `license = {text = "Proprietary"}`. There is **no `LICENSE` file**. Open-source grant is **UNKNOWN** beyond that declaration.

### 1.1 Package / service map

| Path | Role | Evidence |
|---|---|---|
| `supervisor/` | Investigation engine. Public API: `SentinalAISupervisor.investigate()` | `supervisor/agent.py:229`, `:323` |
| `supervisor/phases/` | Live FETCH → CLASSIFY → COLLECT → ANALYZE → PERSIST adapters | `supervisor/agent.py:386-507`; `supervisor/phases/fetch.py`, `classify.py`, `collect.py`, `analyze.py`, `persist.py` |
| `sentinel_core/` | Zero/low-dep models: inference contracts, evidence ledger, intelligence runtime, OIP, EIC | `sentinel_core/models/inference.py`, `sentinel_core/evidence/ledger.py`, `sentinel_core/runtime/pipeline.py` |
| `workers/` | MCP adapters; all tool I/O through `McpGateway.invoke()` | `workers/mcp_client.py:639`; `workers/ops_worker.py:28-32` |
| `knowledge/` | Institutional KG (JSON backend); opt-in via `KNOWLEDGE_GRAPH_ENABLED` | `knowledge/graph_store.py`; `supervisor/agent.py:152` |
| `intelligence/` | Pattern intel, telemetry, ITSM writebacks (flag-gated) | `intelligence/background_runner.py`, `intelligence/itsm_writebacks.py` |
| `agui/` | FastAPI BFF + WebSocket; operator workspace API | `agui/main.py`; `agui/api/investigations.py` |
| `ui/` | React + Vite SPA | `README.md` Getting Started |
| `database/` | Optional PostgreSQL persistence | `database/persistence.py` |
| `eval/`, `sentinelbench/` | Offline scoring corpora and harnesses | `eval/scenarios/`, `sentinelbench/runner.py` |
| `agentcore_runtime.py` | AWS Bedrock AgentCore HTTP contract (`POST /invocations`) | `agentcore_runtime.py:1-11`, `:141` |
| `scripts/run_investigation.py` | CLI | `scripts/run_investigation.py:26-38` |

`supervisor/phases/__init__.py` still describes phases as “scaffolds only”. That comment is **stale**. `investigate()` imports and executes the five phase classes (`supervisor/agent.py:386-507`).

### 1.2 Public interfaces

| Interface | How to invoke | Evidence |
|---|---|---|
| Python API | `SentinalAISupervisor(replay_dir=...).investigate(incident_id, replay=False)` | `supervisor/agent.py:323` |
| CLI | `python scripts/run_investigation.py INC12345 [--json] [--replay]` | `scripts/run_investigation.py` |
| AgentCore HTTP | `POST /invocations` with `{incident_id, replay?}`; `GET /ping` | `agentcore_runtime.py:4-5`, `:141-148` |
| AGUI BFF | `POST /api/v1/investigations`; list/get/graph/risk; WS progress | `agui/api/investigations.py:4-8`, `:50` |
| Replay API | `POST /api/v1/investigations/{id}/replay` | `agui/api/replay.py` |
| Docker (agent) | `Dockerfile` → `entrypoint.sh` → AgentCore runtime on `:8080` | `Dockerfile` |
| Docker (BFF) | `Dockerfile.bff` → `uvicorn agui.main:app` on `:8081` | `Dockerfile.bff` |
| Compose | `docker-compose.yml` (full stack), `docker-compose.agui.yaml` (BFF+UI), `docker-compose.yaml` | compose files |
| Demo host | `render.yaml` → `uvicorn agui.main:app` | `render.yaml` |
| Config | `.env.example` (multi-provider narrative), `.env.template` (Bedrock-only), `supervisor/sentinel_config.py` | see §2.4 |
| Eval CLI | `python scripts/run_evals.py`; `python -m sentinelbench` | `scripts/run_evals.py`; `sentinelbench/__main__.py` |

There is **no** JSON Schema / OpenAPI artifact in-repo that defines a provider-agnostic model config. Config is env-var based.

### 1.3 Four-concern mapping (current)

```
┌─────────────────────────────────────────────────────────────────┐
│ 4. Model / provider layer                                       │
│    supervisor/llm.py  converse() → InferencePort factory        │
│    NullInference | BedrockInference | AnthropicInference |      │
│    OpenAIInference (LLM_PROVIDER; default overlay OFF)          │
│    anthropic SDK also in review_responder / ci_shepherd /       │
│    dev_loop_agent (NOT on the SRE investigate() path)           │
└────────────────────────────┬────────────────────────────────────┘
                             │ converse() / refine_hypothesis()
┌────────────────────────────▼────────────────────────────────────┐
│ 3. Agent orchestration                                          │
│    SentinalAISupervisor.investigate()                           │
│    FETCH → CLASSIFY → COLLECT → ANALYZE → PERSIST               │
│    ExecutionBudget, CircuitBreakerRegistry, FrozenCorpus        │
│    AgenticPlanner (AGENTIC_PLANNER, default off)                │
│    IntelligenceRuntime (ENABLE_INTELLIGENCE_RUNTIME, default off)│
└───────────────┬─────────────────────────────┬───────────────────┘
                │ playbooks / workers         │ evidence dict
┌───────────────▼──────────────┐  ┌───────────▼───────────────────┐
│ 2. Context / tool layer      │  │ 1. SRE intelligence           │
│    McpGateway.invoke()       │  │    classify_incident keywords │
│    workers/*                 │  │    INCIDENT_PLAYBOOKS         │
│    knowledge/ + experience   │  │    _generate_hypotheses       │
│    frozen corpus / replay    │  │    compute_confidence         │
│    MCP stubs when no gateway │  │    _analyze_* type analyzers  │
└──────────────────────────────┘  │    evidence gates G1–G5       │
                                  └───────────────────────────────┘
```

**Architectural fact:** SRE intelligence (concern 1) and MCP tools (concern 2) do not import Anthropic or OpenAI. They call `supervisor.llm.converse` / `is_enabled` or they do not call an LLM at all. Live investigation providers are selected by `LLM_PROVIDER` behind `converse()` (null / bedrock / anthropic / openai).

### 1.4 Investigation / RCA flow (canonical)

Default path (`AGENTIC_PLANNER` off, `LLM_ENABLED` as configured):

1. **Input** — incident id string. CLI (`scripts/run_investigation.py:38`), AgentCore payload (`agentcore_runtime.py:145-148`), or AGUI `StartInvestigationRequest.incident_id` (`agui/api/investigations.py:31-34`).
2. **Frozen corpus capture** — `supervisor.frozen_corpus.capture()` at `investigate()` entry (`supervisor/agent.py:370-373`).
3. **FETCH** — `FetchPhase.execute` loads Moogsoft incident via `ops_worker.get_incident_by_id` (`supervisor/phases/fetch.py:150`; `supervisor/agent.py:1397-1423`; `workers/ops_worker.py:23-32`).
4. **CLASSIFY** — keyword match on summary (`supervisor/tool_selector.py:223-251`); optional LLM fallback only if no keyword hit **and** `LLM_ENABLED=true` (default **false** in `classify_incident`, `:244`).
5. **Context retrieval (parallel with collect)** — ITSM (ServiceNow), Confluence, experience store, knowledge-graph similar, historical memory (`supervisor/phases/classify.py:190+`).
6. **COLLECT** — playbook from `INCIDENT_PLAYBOOKS` (`supervisor/tool_selector.py:29-101`) via `_execute_playbook` (`supervisor/agent.py:1915`). Evidence is a `dict[str, Any]`. Optional `EvidenceLedger` is shadow-only (`sentinel_core/evidence/ledger.py`; `EVIDENCE_LEDGER_SHADOW_ENABLED`).
7. **ANALYZE** — deterministic hypothesis scoring (`supervisor/agent.py:_analyze_evidence` at `:2214`). Optional LLM refine + reasoning (`:2347-2403`). Confidence provenance via `compute_confidence` (`supervisor/helpers/confidence.py`). Gates G2/G3/G5 in `supervisor/evidence_gates.py`.
8. **PERSIST** — observability, optional LLM-as-judge, remediation text, optional proposed fix, DB/memory/KG writes (`supervisor/phases/persist.py`; `supervisor/agent.py:_persist_results` at `:785`).
9. **Output** — dict with at least `root_cause`, `confidence`, `evidence_timeline`, `reasoning` (`supervisor/agent.py:325-328`, result assembly `:2452+`).

### 1.5 Concrete path: `INC12345` (timeout / payment-service)

This is the production-quality fixture in `tests/fixtures/expected_rca_outputs.py:10-16` (`root_cause` keywords: payment-service, database, slow, queries; confidence 90–100). Stub MCP returns fixture data when no gateway is configured (`workers/mcp_client.py:713-715`).

| Step | What happens | Citation |
|---|---|---|
| CLI entry | `SentinalAISupervisor.investigate("INC12345")` | `scripts/run_investigation.py:32-38` |
| Orchestrator | `investigate()` constructs `InvestigationContext`, runs five phases | `supervisor/agent.py:323-507` |
| FETCH | `FetchPhase.execute` → `_fetch_incident` | `supervisor/phases/fetch.py:150`; `supervisor/agent.py:1405-1411` |
| Tool | `OpsWorker._get_incident_by_id` → `McpGateway.invoke("moogsoft.get_incident_by_id", ...)` | `workers/ops_worker.py:23-32` |
| MCP | If `AGENTCORE_GATEWAY_URL` empty and no ARNs: `_stub_response` / `_stub_moogsoft` | `workers/mcp_client.py:704-715`, `:1150` |
| Normalize | `Incident.from_dict` → `to_legacy_dict` | `supervisor/agent.py:1418-1420` |
| CLASSIFY | `classify_incident(summary)` keyword path; timeout keywords include `"timeout"` | `supervisor/phases/classify.py:177`; `supervisor/tool_selector.py:105-108` |
| Playbook | `INCIDENT_PLAYBOOKS["timeout"]`: logs, golden signals, network, latency metrics, changes | `supervisor/tool_selector.py:34-40` |
| COLLECT | `_execute_playbook` dispatches those workers through `_call_worker` → `McpGateway.invoke` | `supervisor/agent.py:1915`; `supervisor/phases/collect.py:236-238` |
| ANALYZE | `_analyze_timeout` among type analyzers; winner via `(-score, name)` sort | `supervisor/agent.py:2745`, `:2379-2386` |
| LLM overlay | If `_llm_enabled()`: `_llm_refine_hypotheses` then `_llm_generate_reasoning` | `supervisor/agent.py:2347-2403`; `supervisor/llm.py:refine_hypothesis` `:277`, `generate_reasoning` `:341` |
| PERSIST | `_persist_results`; replay store if configured | `supervisor/phases/persist.py`; `supervisor/replay.py` |
| Eval key | Expected RCA for this id | `tests/fixtures/expected_rca_outputs.py:10-16` |

With `LLM_ENABLED=false` (CI default), this path is fully deterministic and does not touch Bedrock. That is the **clone-and-run** investigation path today.

### 1.6 Context and memory layer

| Layer | What it is | Flag / default | Citation |
|---|---|---|---|
| Frozen corpus | Snapshot of learning stores at investigation start; hermetic replay | always on in `investigate()` | `supervisor/agent.py:366-373`; `supervisor/frozen_corpus.py` |
| Experience store | RAG-like similar investigations | `EXPERIENCE_STORE_ENABLED` (`.env.example`) | `supervisor/experience_store.py` |
| Knowledge graph (`knowledge/`) | JSON graph of incidents/services/artifacts | `KNOWLEDGE_GRAPH_ENABLED` default false | `supervisor/agent.py:152`; `knowledge/graph_store.py` |
| AgentCore Memory | STM turns + LTM semantic search | `BEDROCK_AGENTCORE_MEMORY_ID`; requires `bedrock-agentcore` SDK | `supervisor/memory.py:1-11`, `:53-71` |
| Intel memory | Deterministic `MemoryRecord` store, no embeddings | documented as not production runtime | `docs/architecture/incident_intelligence_memory.md`; `sentinel_core/intel_memory/` |
| Sentinel wiki | File-based institutional wiki (receipts, patterns, indexes) | `WIKI_PROMOTE_THRESHOLD` | `sentinel_wiki/` |
| Resolution / episodic | Intelligence modules, flag-gated | `ENABLE_INTELLIGENCE_RUNTIME` default off | `sentinel_core/runtime/pipeline.py`; `supervisor/intelligence_runtime.py` |

**Architectural vs config:** the JSON KG and experience store are portable. AgentCore Memory is an **AWS architectural dependency** (SDK + memory resource id), not a config rename.

### 1.7 MCP / tool integrations

All workers must call `McpGateway.invoke()` (`workers/mcp_client.py:7-10`, AGENTS.md). Live path:

```
Worker → McpGateway.invoke()
      → strands MCPClient.call_tool_sync()
      → streamable HTTP
      → AgentCore Gateway
      → target named {Target}___{operation}
      → backend (Moogsoft, Splunk, Sysdig, SignalFx, Dynatrace, ServiceNow, GitHub, Confluence, Kubernetes)
```

Auth priority: OAuth2 client-credentials (Cognito-shaped) → static `GATEWAY_ACCESS_TOKEN` → none (`workers/mcp_client.py:20-26`). Gateway tool names use triple-underscore AgentCore convention (`workers/mcp_client.py:42-45`, `:431-440`).

Hard-coded vendor servers in `_TOOL_TO_SERVER` / `_WORKER_SERVERS`: moogsoft, splunk, sysdig, signalfx, dynatrace, servicenow, github, confluence, kubernetes (`workers/mcp_client.py:351+`; `supervisor/agent.py:237-254`). Playbooks assume those workers exist (`supervisor/tool_selector.py:29-101`).

**Stub fallback is the open-source-friendly path:** `GATEWAY_MODE=stub` (compose default) forces `_stub_response` even if a gateway URL is set (Slice 3, #79). Unset `GATEWAY_MODE` still auto-selects live when `MCP_GATEWAY_URL` / `AGENTCORE_GATEWAY_URL` or ARNs are present. `MCP_GATEWAY_URL` aliases `AGENTCORE_GATEWAY_URL` (Slice 4, #80).

### 1.8 Evidence model

**Runtime authority:** legacy `evidence: dict[str, Any]` keyed by playbook labels / worker actions (`supervisor/phases/collect.py:31-36`, `:124-129`).

**Lifecycle (PB-3):** terminal states `used | filtered | suppressed | unavailable | error` (`supervisor/phases/collect.py:56-117`). Silent drop is forbidden.

**Receipts:** one `Receipt` per worker call (`supervisor/receipt.py:27-57`) with tool, action, params (redacted), timing, status, policy_ref, trace_id.

**Typed ledger:** `EvidenceLedger` / `EvidenceItem` exist (`sentinel_core/evidence/ledger.py`) but do not drive RCA unless shadow flag is on. Analyze does not write the ledger (`supervisor/phases/analyze.py:42-43`).

**Output evidence:** `evidence_timeline` on the result dict; citations via `annotate_citations` (`supervisor/evidence_citation.py`).

### 1.9 Orchestration and safety

- Per-investigation `ExecutionBudget` (`INVESTIGATION_BUDGET_MAX_CALLS` default 20) — `supervisor/guardrails.py:26-28`.
- Wall-clock `INVESTIGATION_DEADLINE_SECONDS` default **120** in `SentinalAISupervisor` (`supervisor/agent.py:232-235`) vs **300** in `.env.example` and **60** as `MAX_INVESTIGATION_TIME_SECONDS` in `.env.template`. Three different knobs; only the class attribute is read by fetch/analyze deadline guards.
- Circuit breakers, call timeout, retries — `supervisor/guardrails.py`.
- Evidence gates G1–G5 — `supervisor/evidence_gates.py`.
- Policy gate — `POLICY_GATE_ENABLED` default false (`workers/mcp_client.py:665`).
- Fail-open per phase, fail-closed on safety gates (`README.md`).
- `ITSM_WRITEBACK_ENABLED` default false.

### 1.10 Prompts

| Prompt | Used for | Citation |
|---|---|---|
| `SUPERVISOR_SYSTEM_PROMPT` | Hypothesis refine + reasoning; encodes SRE protocol, JSON output contract, closed incident-type list | `supervisor/system_prompt.py:7-136`; appended in `supervisor/llm.py:296-302`, `:360-364` |
| `PIL_NARRATION_PROMPT` | Pattern-intel narration; comment says “Call site (future)” | `supervisor/system_prompt.py:151+` |
| Classifier prompt | Exact incident-type token | `supervisor/tool_selector.py:271-278` |
| Planner Think prompt | Next tool JSON | `supervisor/planner.py:143-147`, `:184+` |
| Judge prompt | Eval dimensions JSON | `supervisor/llm_judge.py:42-68` |
| Code-worker prompts | Diff analysis / fix generation | `workers/code_worker.py:156+` |

Structured output is **prompt-enforced JSON**, parsed by `parse_llm_json` (`supervisor/inference_helpers.py:19-59`) or ad-hoc `json.loads` in `code_worker.py:171`. There is **no** native JSON-schema / tool-use API on `converse()`.

### 1.11 Model / LLM dependencies (as implemented)

**Investigation path (must remain provider-independent at the SRE layer):**

- Single door: `supervisor/llm.py` `converse()` / `get_inference_port()` (Slice 1, #77). Tests inject a port via `set_inference_port`.
- Factory (Slice 1 + 2 + 5):
  - `LLM_ENABLED=false` (default, matches CI / `SentinelConfig` / `classify_incident`) or `LLM_PROVIDER=null|none|disabled` → `NullInference`
  - `LLM_PROVIDER=bedrock` → `BedrockInference` (`boto3` Bedrock Converse)
  - `LLM_PROVIDER=anthropic` → `AnthropicInference` (Anthropic Messages; `ANTHROPIC_API_KEY` at call time, never logged)
  - `LLM_PROVIDER=openai` → `OpenAIInference` (OpenAI Chat Completions; `OPENAI_API_KEY` at call time, never logged)
  - any other value → `NullInference` + warning listing `null, bedrock, anthropic, openai`
- Portable model id: `LLM_MODEL` or `BEDROCK_MODEL_ID` (Bedrock-shaped default remains for Bedrock). Anthropic/OpenAI adapters map non-native ids to `claude-sonnet-4-6` / `gpt-4o`.
- Call shape: system text + single user message; temperature + max tokens. **No tools, no streaming, no response_format.**
- Retries: Bedrock botocore adaptive `max_attempts: 2`; Anthropic/OpenAI SDK `max_retries=2`, `timeout=60`. Shared in-process token bucket (`LLM_MAX_CONCURRENT`, `LLM_MAX_CALLS_PER_MIN`).
- Error taxonomy on Anthropic/OpenAI: `rate_limited` / `timeout` / `unknown`. `bedrock_error:*` stays inside the Bedrock adapter.
- Dict shape frozen: `text`, `input_tokens`, `output_tokens`, `model_id`, `latency_ms`, `stop_reason`, optional `error` (`tests/test_inference_contracts.py`).
- Typed contract: `InferencePort`, `InferenceRequest`, `InferenceResponse`, `NullInference` (`sentinel_core/models/inference.py`; `supervisor/inference_helpers.py`).
- Health: `GET /api/v1/health/tools` reports `is_enabled()`, `resolved_provider()`, and the port class — unused API keys do not mark LLM configured (Slice 3).

**Off the SRE path (do not treat as investigation provider):**

- `supervisor/review_responder.py`, `supervisor/ci_shepherd.py`, `supervisor/dev_loop_agent.py` still call `anthropic.Anthropic().messages.create` directly. Out of Slice 5.

**Judge:** `EVAL_JUDGE_MODEL_ID` still defaults to a Bedrock Haiku id and goes through the same `converse()` door (`supervisor/llm_judge.py`).

**Not implemented (still true):** token streaming; native tool-use / constrained decoding; LiteLLM / LangChain.

### 1.12 Configuration surface

Central non-secret config: `SentinelConfig.from_env()` (`supervisor/sentinel_config.py:298-446`). Secrets are intentionally **not** in that object (`:8-10`).

Playbooks: hardcoded `INCIDENT_PLAYBOOKS` is “the source of truth” (`supervisor/tool_selector.py:21-23`). YAML under `config/playbooks/*.yaml` exists; `YAML_PLAYBOOKS_ENABLED` defaults false (`sentinel_config.py:306`).

Tenant overlays: `integrations/tenant_config.py` (`TENANT_CONFIG_PATH`, `DEFAULT_ORG_ID`).

### 1.13 Evals / tests

| Harness | Role | LLM? |
|---|---|---|
| `tests/` pytest (`pyproject.toml` `testpaths = ["tests"]`) | Engine correctness; CI sets `LLM_ENABLED=false` | No in CI |
| `tests/test_determinism.py` | Same input → same classification/scoring; autouse disables LLM | Must stay LLM-off |
| `tests/test_inference_contracts.py` | `converse()` dict shape, `NullInference`, `parse_llm_json` | Mocked Bedrock |
| `tests/test_llm.py`, `test_llm_judge.py`, `test_tool_selector_llm.py` | Provider and fallback behavior | Mocked |
| `tests/fixtures/expected_rca_outputs.py` | Production-quality RCA keys (e.g. INC12345) | Deterministic analyzers |
| `eval/scenarios/001-…005-` | Synthetic incidents with `answer.yml` ground truth | Engine-agnostic scores |
| `sentinelbench/` | Offline scorer; `BenchRunner.run_scenario_with_fixture` does **not** call `investigate()` | No |
| `scripts/run_evals.py` | Runs supervisor on expected incidents; `--llm-judge` uses Bedrock | Optional |
| EIC / gold / enterprise corpora | Documented in README; gold n=3 **underpowered** | N/A |

CI: `.github/workflows/ci.yml` — ruff, mypy (`supervisor workers knowledge`), bandit, pytest 3.11/3.12 with coverage.

### 1.14 Deployment path

- Local: `pip install -r requirements.txt`; `uvicorn agui.main:app --port 8081`; `GATEWAY_MODE=stub` (honored) uses in-process MCP fixtures.
- **AgentCore image:** `Dockerfile` installs `requirements-agentcore.txt` (includes `bedrock-agentcore`, `boto3`, `strands-agents`, `mcp`), non-root user `bedrock_agentcore`, OTEL collector sidecar binary, `EXPOSE 8080`.
- **BFF image:** `Dockerfile.bff` prefers `requirements-agentcore.txt`, serves AGUI on 8081.
- **Compose:** BFF + `agentcore-runtime` + postgres + redis + jaeger + stub-tools (`docker-compose.yml`). `GATEWAY_MODE=stub` (default) forces stubs even though compose sets `AGENTCORE_GATEWAY_URL` (Slice 3).
- **Render demo:** `render.yaml` (auth off, honeypot on).
- AWS region default `us-east-1` in multiple modules.

---

## 2. What prevents plug-and-play / model-agnostic use

Separate **true architectural dependencies** (require code or a substitute component) from **config that can be externalized**.

**Post-Slices 1–5:** investigation LLM overlay is no longer Bedrock-only. Clone-and-run with stubs + `LLM_ENABLED=false` still works. Remaining blockers are legal (LICENSE), optional AWS fabric (AgentCore Memory / AgentCore-shaped MCP for *live* data), and eval power — not a missing OpenAI adapter.

### 2.1 Architectural (must change or wrap — not env-rename)

| Coupling | Status after Slices 1–5 | Evidence |
|---|---|---|
| Investigation LLM door | **SHIPPED** — `converse()` factory + Null/Bedrock/Anthropic/OpenAI | `supervisor/llm.py` `get_inference_port()` |
| Default model id is a Bedrock Claude ID | **Still true** for Bedrock fallback; Anthropic/OpenAI adapters map away | `supervisor/llm.py` `MODEL_ID`, `_to_anthropic_model`, `_to_openai_model` |
| Error taxonomy includes `bedrock_error` | **Still true** inside Bedrock adapter only; Anthropic/OpenAI use `rate_limited` / `timeout` / `unknown` | `sentinel_core/models/inference.py`; `_map_anthropic_error` / `_map_openai_error` |
| AgentCore Memory | **Unchanged** — optional overlay | `supervisor/memory.py`; `BEDROCK_AGENTCORE_MEMORY_ID` |
| AgentCore Gateway as MCP fabric | **Unchanged** for live data; stub path + `GATEWAY_MODE` **SHIPPED** | `workers/mcp_client.py`; Slice 3–4 |
| AgentCore HTTP runtime / Docker user | **Unchanged** (deployment artifact) | `agentcore_runtime.py`; `Dockerfile` |
| Vendor-shaped playbooks | **Mitigated** — YAML aliases behind `YAML_PLAYBOOKS_ENABLED` (default false, Slice 4) | `config/worker_aliases.yaml`; `supervisor/playbook_loader.py` |
| `SUPERVISOR_SYSTEM_PROMPT` operational assumptions | **Unchanged** (SRE content; out of scope) | `supervisor/system_prompt.py` |
| No LICENSE / Proprietary | **Unchanged** — owner deciding separately | `pyproject.toml`; missing `LICENSE` |
| Dual prompt/SDK paths | **Unchanged** — dev-loop still bypasses `InferencePort` | `review_responder.py` |
| Cost table | **Unchanged** — Bedrock Anthropic + Titan prices | `supervisor/eval_metrics.py` |

### 2.2 Config that can be externalized (already mostly env)

Region, model id, temperature, max tokens, `LLM_ENABLED`, `LLM_PROVIDER`, gateway URL (`MCP_GATEWAY_URL` / `AGENTCORE_GATEWAY_URL`), `GATEWAY_MODE`, OAuth client id, per-target names, playbook YAML (flag off), tenant config, feature flags, budget, deadlines, stub vs live MCP.

### 2.3 Docs / config lies that block plug-and-play

Audit-time clone-UX failures and their Slice status:

1. **`.env.example` advertised unimplemented providers.** **SHIPPED** Slice 3 honesty + Slice 2/5 adapters. `.env.example` lists `null \| bedrock \| anthropic \| openai`. Unused keys still do not enable `investigate()`.
2. **`GATEWAY_MODE` unused in Python.** **SHIPPED** Slice 3 — `GATEWAY_MODE=stub` forces stubs even if a URL is set.
3. **`LLM_ENABLED` defaults disagreed (`llm.py` true vs CI false).** **SHIPPED** Slice 1 — `llm.py` default is `"false"`.
4. **Health check theater (any vendor key ⇒ configured).** **SHIPPED** Slice 3 — reports `is_enabled()` + port class.
5. **Compose injects Anthropic into BFF while agent image is Bedrock.** Partially honest: BFF env now includes Anthropic/OpenAI keys, but overlay stays off unless `LLM_ENABLED=true` and `LLM_PROVIDER` selects that adapter. AgentCore image remains AWS-shaped.
6. **`.env.example` vs `.env.template` two products.** **SHIPPED** Slice 3 — `.env.template` is a pointer to `.env.example`.

### 2.4 Environment / org / data-source couplings (clone friction)

| Item | Kind | Evidence |
|---|---|---|
| Default Slack channels `#incidents`, `#sre-intelligence` | org assumption | `supervisor/sentinel_config.py:428-429` |
| Opsgenie default API host | vendor URL | `sentinel_config.py:432` |
| `AWS_REGION` default `us-east-1` | environment | many modules |
| Cognito token URL derivation | AWS IdP | `workers/mcp_client.py:115-117`, `:189-194` |
| AGUI DynamoDB/S3 defaults `agui-state` / `agui-receipts` | AWS storage | `sentinel_config.py:375-376` |
| Eval fixtures `payment-service`, `INC12345` | synthetic, OK | `eval/scenarios/001-payment-timeout/` |
| Notification tokens `PD_TOKEN`, `SN_TOKEN`, … | credentials at call-time | `intelligence/itsm_writebacks.py`, `integrations/notification_router.py` |
| `HONEYPOT_ENABLED` / invite tokens | product access control | `.env.example` §17 |

MCP **stubs** mean a clone can run investigations without those vendors. Live RCA against *their* environment requires either AgentCore-shaped MCP targets or a new gateway adapter — that is a **tool-layer** problem, orthogonal to model-agnosticism, but it blocks “connect to its environment and run” for production data. Slice 4 documents `AGENTCORE_TARGET_*` and YAML worker aliases (`docs/clone/CONNECT_YOUR_ENVIRONMENT.md`).

### 2.5 What is already model-agnostic

Do not rebuild these:

- Keyword classification + playbooks
- `_analyze_*` deterministic analyzers and `compute_confidence`
- Evidence dict + receipts + gates
- `McpGateway` stub/live split (URL-based)
- `InferencePort` / `NullInference` / `parse_llm_json` **wired as the live factory** (Slices 1–5)
- `GATEWAY_MODE` stub/live split (Slice 3)
- MCP URL alias + YAML playbook worker aliases (Slice 4, flag off)
- Frozen corpus + hermetic replay
- SentinelBench / EIC scoring that does not call a provider
- Feature flags defaulting off for planner, writebacks, YAML playbooks

The default investigation is already an SRE engine that **can** run with no model. Model-agnostic LLM overlay is **SHIPPED** for Bedrock, Anthropic, and OpenAI behind `converse()`. Remaining clone friction is LICENSE, live MCP fabric, and optional AgentCore Memory — not a missing third provider.

---

## 3. Target model-agnostic architecture

### 3.1 Product contract (clone → configure → connect → run)

A downstream team should be able to:

1. Clone the repo under an OSS license (**currently blocked** by Proprietary / missing LICENSE — legal, not technical).
2. `pip install -r requirements.txt` and run `pytest` with `LLM_ENABLED=false` (already works in CI).
3. Run `python scripts/run_investigation.py INC12345` against MCP stubs (already works if boto3 missing or LLM disabled).
4. Set **one** provider block in env (not three conflicting files) and run the **same** investigation with LLM overlay against provider A or B.
5. Point `MCP_GATEWAY_URL` (rename of `AGENTCORE_GATEWAY_URL`) at **their** MCP gateway, or keep stubs.
6. Map their log/metrics/ITSM servers in config rather than forking playbooks (YAML playbooks exist but are off).

SRE investigation engine, evidence contracts, context retrieval, MCP integrations, evaluation, and safety/verification stay **provider-independent**.

### 3.2 Layering (target)

```
SRE intelligence  ──unchanged──►  hypotheses, playbooks, confidence, gates
Context / tools   ──unchanged──►  McpGateway + workers + KG + stubs
Orchestration     ──unchanged──►  investigate() phases; may pass InferencePort
Model / provider  ──NEW thin──►  factory → Provider.complete(InferenceRequest)
                                      → InferenceResponse dict (today's converse() shape)
```

**Rule:** `supervisor/agent.py`, `supervisor/phases/*`, `supervisor/tool_selector.py`, `workers/*` (except `code_worker` LLM calls), `knowledge/*` must not import `boto3`, `anthropic`, or `openai`. They depend only on `InferencePort`.

### 3.3 Smallest provider abstraction / configuration boundary

Keep the **existing** `InferencePort` signature as the SRE-facing API (`sentinel_core/models/inference.py:109-124`):

```text
complete(system_prompt, user_message, model_id=None, temperature=None, max_tokens=None)
    -> dict {text, input_tokens, output_tokens, model_id, latency_ms, stop_reason, error?}
```

Add a **factory** behind `supervisor.llm.converse` so every current caller (`refine_hypothesis`, `generate_reasoning`, `classify_incident_llm`, `AgenticPlanner._think`, `llm_judge`, `code_worker`, `self_critique`, `memory_compression`, `agent_harness`) is provider-agnostic **without changing SRE-domain code**.

Config (single source; `SentinelConfig` should own these — today it does not even store `BEDROCK_MODEL_ID`):

```text
LLM_ENABLED=false|true
LLM_PROVIDER=null|bedrock|anthropic|openai   # null = NullInference
LLM_MODEL=<provider-native id>
# provider credentials read at call-time, never logged:
#   AWS default chain | ANTHROPIC_API_KEY | OPENAI_API_KEY
LLM_TEMPERATURE=0.0
LLM_MAX_TOKENS=2048
```

Default for clone-and-run: `LLM_PROVIDER=null` (or `LLM_ENABLED=false`). That preserves CI determinism.

### 3.4 Normalization map (required capabilities)

| Capability | Today | Target normalization | Notes |
|---|---|---|---|
| **Text complete** | Bedrock `converse` messages+system | `InferencePort.complete` | Already the contract |
| **Tool calling** | **Not used** for investigation LLM. Tools are MCP workers, not LLM tools. Planner asks the model for JSON `{worker, action, params}` and the orchestrator executes MCP. | **Do not add native tool-use in slice 1.** Keep JSON-planned MCP. If a future provider-native tool-use is added, map to the same `{worker, action, params}` schema in the provider adapter, not in SRE code. | `supervisor/planner.py:137-155`; `converse()` has no `tools` argument |
| **Structured output** | Prompt “Respond in JSON” + `parse_llm_json` / `json.loads` | Keep prompt JSON as the **portable** path. Optional provider feature (`response_format` / Bedrock constrained decoding) lives **inside** the adapter; on failure fall back to `parse_llm_json`. Callers already handle `StructuredResult.ok is False` by keeping prior hypotheses. | `supervisor/inference_helpers.py`; `supervisor/llm.py:327-330` |
| **Context limits** | `LLM_MAX_TOKENS`; code worker truncates diffs (`CODE_WORKER_MAX_DIFF_CHARS` default 8000) | Adapter reports `context_window` + `max_output_tokens`. Callers already truncate. Do not make SRE code count tokens per vendor. | `workers/code_worker.py` env; `supervisor/llm.py:43` |
| **Retries** | Botocore adaptive, 2 attempts; plus in-process rate limiter | Provider-agnostic retry wrapper around `complete()`: retry on timeout / 429 / 5xx; map to `InferenceError.RATE_LIMITED` / `TIMEOUT`. Bedrock-specific `ClientError` codes stay inside the Bedrock adapter. | `supervisor/llm.py:125-128`, `:246-270` |
| **Streaming** | LLM: none. UI: investigation **event** stream (`supervisor/progress_stream.py`), not tokens. | **Do not add token streaming in slice 1.** If added later, `complete_stream()` is optional on the port; SRE path stays buffered `complete()`. | `supervisor/progress_stream.py:1-37` |
| **Provider-specific behavior** | Bedrock system=`[{text}]`; Anthropic Messages; OpenAI Chat Completions | Adapter translates **to/from** `InferenceRequest`/`InferenceResponse`. Stop reasons mapped to a small enum (`end_turn`, `max_tokens`, `disabled`, `error`). `model_id` echoed as the **native** id for telemetry, not rewritten into SRE logic. | |
| **Disabled / test** | `_disabled_response()`; `NullInference` | `LLM_PROVIDER=null` uses `NullInference`. Tests keep patching `converse` or injecting NullInference. | `supervisor/llm.py:392-401`; `tests/test_inference_contracts.py` |

### 3.5 What must remain provider-independent

- Playbook selection and worker execution
- Hypothesis generation/scoring/`compute_confidence`
- Evidence lifecycle and gates
- Frozen corpus / replay byte-identity with LLM off
- MCP stub + live gateway
- Eval keyword/confidence scoring (`scripts/run_evals.py` rule-based path; `sentinelbench`)
- Safety: writeback flags, policy gate, budgets

LLM overlay is **additive**: if `complete()` errors, current code already keeps pre-LLM hypotheses (`supervisor/agent.py:2366-2371`; `supervisor/llm.py:319-325`).

---

## 4. Gap analysis

| Gap | Severity | Concern | Minimum fix |
|---|---|---|---|
| `converse()` = Bedrock only | High (for model-agnostic goal) | 4 | Factory + 2 adapters behind existing port |
| Docs claim Anthropic/OpenAI work | High (clone UX) | 4 / docs | Align `.env.example` with runtime **or** implement adapters |
| `GATEWAY_MODE` unused | High (clone UX / live data) | 2 / docs | Honor it in `McpGateway.invoke` **or** delete from docs/compose |
| `LLM_ENABLED` default split | Medium | 4 | One default: false (match CI, `SentinelConfig`, classifier) |
| `SentinelConfig` omits provider/model | Medium | 4 | Add `llm_provider`, `llm_model` to `SupervisorConfig` |
| AgentCore Memory for LTM | Medium (optional feature) | 2 | Keep flag-off; document JSON KG as the portable memory |
| Vendor-hardcoded playbooks | Medium (connect-your-env) | 1–2 | Enable/document `YAML_PLAYBOOKS_ENABLED` + worker alias map |
| No OSS license | High (legal plug-and-play) | product | Add LICENSE (owner decision) — **UNKNOWN** which license |
| Dual Anthropic Messages clients | Low for RCA | 4 | Out of slice 1; later route through InferencePort or isolate package |
| `code_worker` uses raw `json.loads` | Low | 4 | Use `parse_llm_json` (normalization only) |
| Judge default Haiku Bedrock id | Low | 4 | `EVAL_JUDGE_MODEL_ID` already env-overridable |
| Compose always sets gateway URL | Medium | 2 | Don’t set URL when mode=stub |
| Root `ARCHITECTURE_AUDIT.md` stale | Low | docs | Point to this document |
| Gold eval n=3 | Validation, not model-agnostic | eval | Known; README already says underpowered |
| Production MTTI reduction | UNKNOWN | product | `NOT_MEASURED` |

---

## 5. Prioritized migration plan (smallest steps first)

Do **not** change SRE-domain files (`investigate()` phases, playbooks, prompts, analyzers, evidence gates) in any remaining slice.

### Slice 0 — Honesty (docs/config only, no behavior change if flags stay off) — **SHIPPED #76**

1. Document the real LLM client and the real MCP switch.
2. Mark `.env.example` multi-provider block as **intended**, not implemented. *(Superseded by Slices 2–5 adapters + Slice 3 honesty.)*

### Slice 1 — Unify the investigation LLM door — **SHIPPED #77**

1. `supervisor.llm.converse` is a facade: resolve `InferencePort` from env (`null` / Bedrock).
2. `LLM_ENABLED` defaults false in `llm.py`.
3. `llm_provider` / `llm_model` on `SentinelConfig`.
4. Dict shape frozen.
5. No SRE-domain edits.

**Exit met:** `LLM_PROVIDER=null` and `LLM_PROVIDER=bedrock` both run `investigate()`; with LLM off, INC12345 matches expected RCA.

### Slice 2 — Second provider behind the same door — **SHIPPED #78**

1. `AnthropicInference` implements `InferencePort` only.
2. Credentials, max tokens, stop reasons, retries inside the adapter.
3. No prompt / planner / analyzer changes.
4. Acceptance tests §6.2 A–D.

### Slice 3 — Config and clone UX — **SHIPPED #79** (LICENSE left to owner)

1. `GATEWAY_MODE=stub` honored in `McpGateway`.
2. Single `.env.example`; `.env.template` is a pointer.
3. Health check reports the actual port class.
4. LICENSE decision (owner) — **still open**.

### Slice 4 — Connect-your-environment — **SHIPPED #80**

1. `AGENTCORE_TARGET_*` documented.
2. YAML playbooks + `config/worker_aliases.yaml` behind `YAML_PLAYBOOKS_ENABLED` (default false).
3. `MCP_GATEWAY_URL` aliases `AGENTCORE_GATEWAY_URL`.

### Slice 5 — Third provider — **this PR**

- `OpenAIInference` Chat Completions behind the same `converse()` door.
- Same taxonomy as Anthropic (`rate_limited` / `timeout` / `unknown`).
- `OPENAI_API_KEY` at call time, never logged.
- Factory warning list: `null, bedrock, anthropic, openai`.
- Acceptance A–D extended (canned OpenAI port + factory D).

**Still out of scope (do not pre-build):** native structured-output / tool-use; token streaming; portable LTM to replace AgentCore Memory; routing dev-loop Anthropic clients through InferencePort; whole-repo ruff/mypy/bandit (pre-existing / UNKNOWN).

**Anti-goals:** rewriting `investigate()`; introducing LangChain/LiteLLM as a new framework; making playbooks LLM-only; enabling writebacks.

---

## 6. How we prove it (acceptance tests)

### 6.1 Existing behavior that must remain unchanged

These are **contracts**. Provider work that breaks them is a failed migration.

| Contract | Evidence | Must remain |
|---|---|---|
| Deterministic classify/score with LLM off | `tests/test_determinism.py` (autouse `LLM_ENABLED=false`) | Pass unmodified |
| `converse()` dict keys | `tests/test_inference_contracts.py` “Backward compatibility: converse() dict shape unchanged” | Same keys |
| INC12345 expected RCA (LLM off) | `tests/fixtures/expected_rca_outputs.py:10-16`; supervisor tests using that fixture | Same keywords/confidence band |
| Keyword classifier does not call LLM when a keyword matches | `supervisor/tool_selector.py:237-241`; `tests/test_tool_selector_llm.py` | Unchanged |
| Agentic planner default off | `AGENTIC_PLANNER` default false; `tests/test_collect_phase.py` | Unchanged |
| ITSM writeback default off | `ITSM_WRITEBACK_ENABLED` default false; AGENTS.md | Unchanged |
| MCP stub when no URL | `workers/mcp_client.py:713-715`; worker tests | Unchanged |
| CI `LLM_ENABLED=false` | `.github/workflows/ci.yml:10` | Unchanged |
| Frozen corpus / hermetic replay | `tests/frozen_corpus/` (as referenced by README) | Unchanged |
| Scoring purity | `tests/test_scoring_purity.py` (cited by `memory/warm/rca_patterns.md`) | Unchanged |
| Synthetic harness bans live SDKs | `tests/synthetic/test_synthetic_runner.py` bans `boto3`/`openai`/`anthropic` in that path | Unchanged |

### 6.2 Acceptance tests (added with Slices 2 and 5; not the audit PR)

**A–D SHIPPED** in `tests/test_slice2_inference_providers.py` (Anthropic Slice 2, OpenAI extended Slice 5). Inject via `set_inference_port` — do not patch boto3 in the SRE test.

**A. Same investigation, two providers, no SRE-domain edits**

- Fixture: `eval/scenarios/001-payment-timeout/` (or stub `INC12345`).
- Fix `LLM_ENABLED=true`, `AGENTIC_PLANNER=false`, MCP stubs.
- Inject `InferencePort` (do not patch boto3 in the SRE test — patch the factory).
- Run `investigate(incident_id)` twice:
  - Provider A: canned `InferenceResponse` with valid refine JSON + reasoning text.
  - Provider B: different client class, **identical** canned `InferenceResponse`.
- Assert: `root_cause`, `confidence`, `evidence_timeline`, playbook worker call list are equal.
- Assert: git diff of files under `supervisor/phases/`, `supervisor/tool_selector.py`, `supervisor/helpers/`, `workers/ops_worker.py` (etc.) is empty in that PR except `supervisor/llm.py` + new `supervisor/providers/` (or equivalent) + tests.

**B. Provider failure equals today’s fail-open**

- Provider raises / returns `{error: ...}`.
- Assert hypotheses remain pre-LLM scores (`supervisor/agent.py:2366-2371` behavior).
- Assert investigation still returns a result (not an exception).

**C. Null provider equals CI path**

- `LLM_PROVIDER=null` or `LLM_ENABLED=false`.
- Assert no network; assert `tests/test_determinism.py` still passes.

**D. Config selects provider without code change**

- Env `LLM_PROVIDER=bedrock` vs `LLM_PROVIDER=<second>` changes only which adapter class `converse()` uses (unit-level).
- `SentinelConfig` exposes the choice.

**E. Eval still provider-agnostic**

- `python -m sentinelbench` / rule-based `scripts/run_evals.py` do not import the second provider SDK.
- Optional `--llm-judge` uses `InferencePort` (not a raw Bedrock client).

**F. MCP independence (regression)**

- With LLM overlay on, workers still only call `McpGateway.invoke`.
- Ban test: investigation path must not call `anthropic.Anthropic` or `openai.OpenAI` from `supervisor/agent.py` / phases.

### 6.3 Out-of-scope proof (do not fake)

Whether two **live** cloud providers produce equally *correct* RCA on production incidents is **UNKNOWN** until a powered eval corpus exists (gold n=3 today). Acceptance tests above prove **plug-in mechanics and fail-open**, not equal intelligence quality.

---

## 7. VERIFY notes

- Phase package comment vs live `investigate()`: **resolved in favor of source** (`agent.py:386-507`).
- README “five-phase” description is correct; README `GATEWAY_MODE` claim is **not** backed by Python.
- `ARCHITECTURE_AUDIT.md` (2026-03) describes a pre-phase `agent.py` flow; use this document for model-agnostic planning.
- Test count “5,982 passed” is from README/`PROJECT_STATUS.md`, not re-run in this audit — treat as **UNKNOWN at audit time**.
- Whether any production tenant runs live AgentCore + Bedrock with this codebase: **UNKNOWN** (no production telemetry in repo; `eval/scientific_validation/report.json` exists but was not used as a live-ops proof).
- Whether `strands` MCPClient can talk to a non-AgentCore MCP server: **UNKNOWN** (code assumes AgentCore URL + `Target___op` names).

---

## 8. LEARN (reusable decisions)

Recorded in `memory/warm/operational_decision_ledger.md` and summarized here:

1. **SRE engine is already model-optional.** Default CI path is the clone path. Do not make LLM mandatory for RCA.
2. **`InferencePort` is the abstraction; do not add a new framework.** Implement providers behind `converse()`.
3. **MCP tools ≠ LLM tools.** Planner JSON + `McpGateway` stays; native model tool-calling is not required for model-agnostic RCA.
4. **Structured output stays parse-then-fallback.** Native JSON mode is an adapter optimization.
5. **`GATEWAY_MODE` is clone UX, not model-agnosticism** — Slice 3 implemented it; do not couple it to provider adapters.
6. **Third provider is copy-the-adapter, not a new door.** OpenAI Chat Completions maps to the same frozen converse dict and the same Anthropic error taxonomy. Keep credentials call-time and unlogged.
7. **Do not “fix” pre-existing CI lint in a provider PR.** ruff/mypy/bandit debt on `main` is UNKNOWN / pre-existing; Slice 5 is not a whole-repo cleanup.
8. **LICENSE and AgentCore Memory stay owner/optional.** Neither blocks claiming “investigation LLM overlay is model-agnostic.”

---

## 9. Shortest evidence-backed path (summary)

| Goal | Shortest path |
|---|---|
| Another team runs RCA tomorrow | Stubs + `LLM_ENABLED=false` + CLI/pytest (**already true**) |
| Same RCA with LLM overlay, two/three models | **SHIPPED** Slices 1–2 + 5: factory + Anthropic + OpenAI adapters; tests A–D |
| They connect *their* Splunk/ITSM | **SHIPPED** Slice 3–4 docs/config; live data still needs a real MCP gateway |
| They treat this as OSS | Owner adds LICENSE; **UNKNOWN** here |
| Production-ready live ops | Still requires real gateway, secrets, and a powered eval — see certification docs; model-agnosticism does not unblock that |
| Token streaming / native tool-use / AgentCore Memory replacement | Explicitly **out of scope** |

**Minimum changes to claim “model-agnostic investigation LLM”:** Slice 1 + Slice 2 + tests A–D — **met on main**. Slice 5 adds the third provider without touching SRE logic. No playbook, analyzer, MCP, or prompt rewrite.
