# Model / Environment Coupling Table

Companion to [`model_agnostic_sre_agent.md`](model_agnostic_sre_agent.md).  
**Rule:** every row cites source. Kind is `architectural` (needs a substitute component or code) vs `config` (env/file can change it without SRE logic changes) vs `docs-drift` (documented but not implemented) vs `test-only`.

SRE-domain files (`supervisor/agent.py` analyzers, `supervisor/tool_selector.py` playbooks, `supervisor/helpers/confidence.py`, evidence gates) should not appear as *model* couplings unless they call `converse()` / `is_enabled()`.

---

## A. Model providers, SDKs, model IDs

| ID | Coupling | Kind | Location | Notes |
|---|---|---|---|---|
| M1 | `boto3` Bedrock Runtime client | architectural | `supervisor/llm.py:27-34`, `:122-131` | Optional import; investigation LLM dead without it when enabled |
| M2 | `client.converse(modelId=..., messages=..., system=..., inferenceConfig=...)` | architectural | `supervisor/llm.py:204-217` | Bedrock-specific request shape |
| M3 | Default `BEDROCK_MODEL_ID=anthropic.claude-sonnet-4-5-20250929-v1:0` | config + hard default | `supervisor/llm.py:41`; `.env.template:9` | Env-overridable; default is Bedrock Claude |
| M4 | `LLM_ENABLED` default `"true"` in `llm.py` | config inconsistency | `supervisor/llm.py:44` vs `supervisor/sentinel_config.py:304` default false vs `tool_selector.py:244` default false vs CI false | Architectural risk: empty env + boto3 installed → live Bedrock |
| M5 | `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` | config | `supervisor/llm.py:42-43` | Portable |
| M6 | `LLM_MAX_CONCURRENT`, `LLM_MAX_CALLS_PER_MIN` | config | `supervisor/llm.py:46-47` | In-process limiter; portable |
| M7 | `AWS_REGION` default `us-east-1` | config | `supervisor/llm.py:40`; `workers/mcp_client.py:100`; `supervisor/memory.py:54` | AWS environment assumption |
| M8 | `InferenceError.BEDROCK_ERROR` | architectural (taxonomy) | `sentinel_core/models/inference.py:19` | Portable port with Bedrock-named error |
| M9 | Error string `bedrock_error: {code}` | architectural | `supervisor/llm.py:252-254` | Callers generally check `error` truthiness |
| M10 | `EVAL_JUDGE_MODEL_ID` default Haiku Bedrock id | config + hard default | `supervisor/llm_judge.py:31` | Uses same `converse()` |
| M11 | Judge docstring “Bedrock Converse API” | docs | `supervisor/llm_judge.py:1-4`, `:12-14` | |
| M12 | `LLM_PROVIDER` read only in AGUI health | docs-drift | `agui/main.py:238` | Not read by `supervisor/llm.py` |
| M13 | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `BEDROCK_REGION` health probe | docs-drift | `agui/main.py:239-247` | Does not configure `converse()` |
| M14 | `.env.example` Anthropic as Option A | docs-drift | `.env.example:17-30` | Conflicts with `llm.py` |
| M15 | `.env.example` `LLM_MODEL=claude-sonnet-4-6` / `gpt-4o` | docs-drift | `.env.example:19-23` | Runtime uses `BEDROCK_MODEL_ID` |
| M16 | `LLM_MODEL_ID` recorded on artifacts | config (unused by client) | `supervisor/artifact_writer.py:59` | Different name than `BEDROCK_MODEL_ID` |
| M17 | `anthropic` package required in `requirements.txt` | architectural (dep) | `requirements.txt:2`; `pyproject.toml:13` | Used by dev-loop, not `converse()` |
| M18 | `openai` in `pyproject.toml` deps | architectural (dep) | `pyproject.toml:18` | No investigation caller found |
| M19 | `openai` commented optional in `requirements.txt` | config | `requirements.txt:17` | Split packaging |
| M20 | `anthropic.Anthropic().messages.create` | architectural (side path) | `supervisor/review_responder.py:346-351`; `supervisor/ci_shepherd.py:221+`; `supervisor/dev_loop_agent.py:595+` | `DEV_LOOP_MODEL` default `claude-sonnet-4-6`; **not** SRE `investigate()` |
| M21 | Cost table Anthropic Claude 3.x + Amazon Titan | architectural (FinOps) | `supervisor/eval_metrics.py:484-491` | Unknown models use Sonnet-like default `:493-494` |
| M22 | `strands-agents` + `mcp` SDKs | architectural (tools, not LLM) | `workers/mcp_client.py:77-83`; `requirements-agentcore.txt:20-22`; `pyproject.toml:16,29` | AgentCore MCP client |
| M23 | `bedrock-agentcore` SDK | architectural | `requirements-agentcore.txt:2`; `agentcore_runtime.py:109`; `supervisor/memory.py:35-40` | Runtime + Memory |
| M24 | Keywords `bedrock`, `agentcore` on package | docs | `pyproject.toml:11` | Marketing/metadata coupling |
| M25 | `NullInference` / `InferencePort` | already portable | `supervisor/inference_helpers.py:62-105`; `sentinel_core/models/inference.py:109-124` | Not wired as live factory |
| M26 | `converse_typed()` | portable wrapper | `supervisor/llm.py:404-420` | Still calls Bedrock `converse()` |
| M27 | OTEL GenAI attrs `GENAI_REQUEST_MODEL` | portable telemetry | `supervisor/agent.py:38,702`; `supervisor/observability.py` | Values come from Bedrock ids today |
| M28 | `docker-compose.yml` `ANTHROPIC_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL` | docs-drift | `docker-compose.yml:41-43` | BFF env; agent LLM is Bedrock |
| M29 | Tests hard-code Bedrock model ids | test-only | `tests/test_eval_pipeline.py:197`, `:239`; `tests/test_agent_coverage.py:102`; `tests/test_eval_metrics_coverage.py:214` | Fixtures, not runtime |
| M30 | Synthetic tests **ban** `boto3`/`openai`/`anthropic` | test-only (good) | `tests/synthetic/test_synthetic_runner.py:166-174`; similar in replay/hypothesis/causal/strategy tests | Keep |

---

## B. Investigation LLM call sites (all should go through InferencePort)

| ID | Call site | Function | Kind |
|---|---|---|---|
| C1 | `supervisor/llm.py:317`, `:377` | `refine_hypothesis`, `generate_reasoning` | SRE overlay |
| C2 | `supervisor/agent.py:64-67`, `:2349`, `:2393` | `_llm_enabled`, `_llm_refine_hypotheses`, `_llm_generate_reasoning` | SRE overlay |
| C3 | `supervisor/tool_selector.py:268-287` | `classify_incident_llm` | SRE overlay (fallback) |
| C4 | `supervisor/planner.py:56`, `:143` | `AgenticPlanner._think` | opt-in orchestration |
| C5 | `supervisor/agent.py:1882` | `_execute_planner_loop` passes `converse` | opt-in |
| C6 | `supervisor/llm_judge.py:26`, `:121` | judge scoring | eval overlay |
| C7 | `supervisor/self_critique.py:366` | critique narrative | overlay; `CRITIQUE_LLM_ENABLED` |
| C8 | `workers/code_worker.py:156`, `:357` | diff analysis / fix gen | worker overlay |
| C9 | `supervisor/remediation.py:315` | `enrich_remediation_llm` | overlay |
| C10 | `supervisor/memory_compression.py:209` | compression | overlay |
| C11 | `supervisor/agent_harness.py:679` | reflection | overlay; `HARNESS_REFLECTION_LLM` |
| C12 | `supervisor/loop_controller.py` (via `llm_fn`) | same as planner | opt-in |

None of these implement a second HTTP client except C8–C11 still using `converse()`. Dev-loop `messages.create` is **not** in this table (see M20).

---

## C. MCP / tools / vendors

| ID | Coupling | Kind | Location |
|---|---|---|---|
| T1 | `AGENTCORE_GATEWAY_URL` as live switch | architectural name, config value | `workers/mcp_client.py:103`, `:704-706` |
| T2 | `GATEWAY_MODE` | docs-drift | `.env.example:49`; `docker-compose.yml:19,39`; **zero Python reads** |
| T3 | OAuth2 + Cognito domain / user pool | architectural (AWS IdP) | `workers/mcp_client.py:108-117`, `:189-198` |
| T4 | `GATEWAY_OAUTH2_SECRET_ARN` Secrets Manager | architectural (AWS) | `workers/mcp_client.py:114`; `agentcore_runtime.py:92-98` |
| T5 | `GATEWAY_ACCESS_TOKEN` static JWT | config | `workers/mcp_client.py:106` |
| T6 | Tool ARN map `MCP_*_TOOL_ARN` | config (legacy AWS) | `workers/mcp_client.py:122-131` |
| T7 | Gateway name `{Target}___operation` | architectural | `workers/mcp_client.py:42-45`, `:431-440` |
| T8 | Default target names `MoogsoftTarget`, `SplunkTarget`, … | config | `workers/mcp_client.py:411-419`; `sentinel_config.py:405-413` |
| T9 | `_TOOL_TO_SERVER` vendor catalog | architectural (tool model) | `workers/mcp_client.py:351+` |
| T10 | `_WORKER_SERVERS` vendor mapping | architectural | `supervisor/agent.py:237-254` |
| T11 | Stub fallback when no URL/ARN | portable (good) | `workers/mcp_client.py:713-715` |
| T12 | Per-server RPM limits | config | `workers/mcp_client.py:448-455` |
| T13 | `MCP_CALL_TIMEOUT`, `MCP_MAX_RETRIES`, `MCP_DEDUP_ENABLED` | config | `workers/mcp_client.py:134-135`, `:679` |
| T14 | `strands.tools.mcp.MCPClient` + `streamablehttp_client` | architectural | `workers/mcp_client.py:77-79` |
| T15 | ThousandEyes RCA | config (off) | `ENABLE_THOUSANDEYES_RCA`; `workers/network_worker.py`; `integrations/thousandeyes/` |
| T16 | Playbooks assume Splunk/APM/ITSM workers | architectural (SRE content, vendor-shaped) | `supervisor/tool_selector.py:29-101` |
| T17 | YAML playbooks exist, flag off | config | `config/playbooks/*.yaml`; `YAML_PLAYBOOKS_ENABLED` default false |
| T18 | Compose sets `AGENTCORE_GATEWAY_URL` always | config footgun | `docker-compose.yml:38` vs unused `GATEWAY_MODE` |

---

## D. Memory / persistence / AWS runtime

| ID | Coupling | Kind | Location |
|---|---|---|---|
| R1 | AgentCore Memory STM/LTM | architectural | `supervisor/memory.py`; `BEDROCK_AGENTCORE_MEMORY_ID` |
| R2 | `BedrockAgentCoreApp` runtime | architectural | `agentcore_runtime.py:109-112` |
| R3 | Docker user `bedrock_agentcore` | architectural (image) | `Dockerfile:30-31` |
| R4 | `requirements-agentcore.txt` | architectural | that file |
| R5 | AGUI DynamoDB table / S3 bucket defaults | config (AWS) | `sentinel_config.py:375-376` |
| R6 | `DATABASE_URL` Postgres + pgvector | config | `sentinel_config.py:382`; `database/persistence.py` |
| R7 | JSON KG `KNOWLEDGE_STORAGE_DIR` | portable | `knowledge/graph_store.py:26-29` |
| R8 | SQLite `OPS_DB_PATH` default `eval/ops_intelligence.db` | config | `sentinel_config.py:388` |
| R9 | Replay dir default `/tmp/sentinalai_replays` | config | `scripts/run_investigation.py:32` |
| R10 | `AWS_ACCESS_KEY_ID` in compose AGUI | config | `docker-compose.agui.yaml:35-36` |

---

## E. Org, product, and operational assumptions

| ID | Coupling | Kind | Location |
|---|---|---|---|
| O1 | System prompt: “deployed inside a production environment”; PagerDuty priors | prompt/org | `supervisor/system_prompt.py:7-26` |
| O2 | Slack `#incidents`, `#sre-intelligence` | config | `sentinel_config.py:428-429` |
| O3 | Opsgenie `https://api.opsgenie.com/v2` | config | `sentinel_config.py:432` |
| O4 | `DEFAULT_ORG_ID=default` | config | `integrations/tenant_config.py:40`; `sentinel_config.py:434` |
| O5 | `ENVIRONMENT` in {development, staging, production} | config | `sentinel_config.py:478-481` |
| O6 | `pyproject.toml` license Proprietary; no LICENSE file | legal/architectural | `pyproject.toml:10` |
| O7 | AGUI Cognito JWKS URL | config | `sentinel_config.py:370` |
| O8 | `AUTH_REQUIRED` default false on AgentCore; `AGUI_AUTH_REQUIRED` default true | config | `agentcore_runtime.py:36`; `sentinel_config.py:368` |
| O9 | Feature flags default off: planner, writebacks, YAML playbooks, intelligence runtime | config (good) | `sentinel_config.py`; AGENTS.md |
| O10 | `INVESTIGATION_DEADLINE_SECONDS` default 120 vs `.env.example` 300 vs `.env.template` `MAX_INVESTIGATION_TIME_SECONDS=60` | config drift | `supervisor/agent.py:232-235` |
| O11 | Notification router vendor APIs | config | `integrations/notification_router.py` |
| O12 | `ITSM_WRITEBACK_ENABLED` default false | config (safety) | `intelligence/itsm_writebacks.py:27` |
| O13 | Eval services named `payment-service` | test fixture | `eval/scenarios/001-payment-timeout/scenario.yml` |
| O14 | `render.yaml` demo with auth off | deployment | `render.yaml` |
| O15 | Honeypot / invite tokens | product | `.env.example` §17; `agui/middleware/honeypot.py` |

---

## F. Prompts and structured output (provider-sensitive but SRE-owned)

| ID | Item | Kind | Location |
|---|---|---|---|
| P1 | `SUPERVISOR_SYSTEM_PROMPT` JSON output contract | SRE-owned prompt | `supervisor/system_prompt.py:105-124` |
| P2 | Temperature 0 instruction in prompt | prompt vs `LLM_TEMPERATURE` | `supervisor/system_prompt.py:128` |
| P3 | “≤ 60 seconds wall time” in prompt vs 120s engine deadline | prompt drift | `system_prompt.py:129` vs `agent.py:232-235` |
| P4 | Classifier “ONLY the incident type” | portable | `tool_selector.py:271-278` |
| P5 | Planner JSON schema in prompt | portable (not native tools) | `planner.py:143-147` |
| P6 | `parse_llm_json` fence stripping | portable | `inference_helpers.py:16-59` |
| P7 | `code_worker` `json.loads` after fence strip | should use P6 | `workers/code_worker.py:165-171` |

Changing P1–P5 is **not** required for model-agnosticism. Adapters must tolerate imperfect JSON (already true).

---

## G. Explicit non-couplings (do not “fix”)

| Item | Why it is not a model coupling |
|---|---|
| Keyword `classify_incident` | No LLM unless fallback |
| `INCIDENT_PLAYBOOKS` / `_analyze_*` | Deterministic SRE intelligence |
| `compute_confidence` | Pure function |
| Evidence dict / receipts / gates | Provider-independent |
| `McpGateway` stubs | Allow clone without vendors |
| `NullInference` | Already a second “provider” for tests |
| SentinelBench fixture runner | Does not call `investigate()` or LLMs |
| CI `LLM_ENABLED=false` | Guards the deterministic core |

---

## H. Docs that contradict source (fix in slice 0–3)

| Doc | Claim | Source reality |
|---|---|---|
| `.env.example` §1 | Anthropic/OpenAI/Bedrock selectable | Only Bedrock `converse()` |
| `README.md` live data | `GATEWAY_MODE=stub` default | Env var unused; URL/ARN decide |
| `docs/certification/PRODUCT_READINESS_AUDIT.md` | same GATEWAY_MODE claim | same |
| `supervisor/phases/__init__.py` | phases are scaffolds | `investigate()` runs them |
| `ARCHITECTURE_AUDIT.md` (2026-03) | pre-phase agent flow | superseded |
| `agui` health | LLM ready if any vendor key set | Keys unused by `llm.py` |
| `Dockerfile` comments | `cp .env.template` | `.env.example` is the other template |

---

## I. UNKNOWN (not provable from repo)

- Whether a live Anthropic/OpenAI investigation client existed on an unmerged branch
- Whether `strands` MCPClient works against a generic MCP server without AgentCore naming
- Production tenants, traffic, or Bedrock quotas
- Intended open-source license
- Current pytest count (README 5,982 not re-measured this audit)
- Whether `LLM_MODEL` vs `BEDROCK_MODEL_ID` vs `LLM_MODEL_ID` was a planned rename
