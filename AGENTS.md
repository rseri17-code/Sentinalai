# AGENTS.md — SentinalAI Developer Briefing

## Setup

```bash
pip install -r requirements.txt
# Start the AGUI (agent UI + BFF):
docker compose -f docker-compose.agui.yaml up --build
# Or run the BFF directly:
uvicorn agui.bff:app --reload --port 8000
```

Tests:
```bash
python3 -m pytest tests/ -q                        # full suite
python3 -m pytest tests/test_supervisor.py -q      # supervisor only
python3 -m pytest sentinalbench/ -q                # benchmark harness
```

## Directory Structure

| Path | Purpose |
|---|---|
| `supervisor/` | Orchestration core: `agent.py` (SentinalAISupervisor), `planner.py` (AgenticPlanner), `llm.py`, `guardrails.py`, `tool_selector.py` |
| `workers/` | MCP tool adapters: `mcp_client.py` (McpGateway), `ops_worker.py`, `log_worker.py`, `metrics_worker.py`, `apm_worker.py`, `itsm_worker.py`, `devops_worker.py`, `confluence_worker.py`, `git_worker.py`, `code_worker.py` |
| `intelligence/` | ML/analytics: `causal_graph.py`, `episodic_memory.py`, `topology_learner.py`, `itsm_writebacks.py` |
| `sentinalbench/` | Benchmark harness: `score.py`, `loader.py`, scenario runner |
| `eval/` | Eval scenarios: `eval/scenarios/<name>/` each with `scenario.yml`, `answer.yml`, `alert.json`, `evidence/` |
| `ui/` | Streamlit demo UI |
| `agui/` | Agent UI: `bff.py` (FastAPI BFF), `replay_engine.py`, WebSocket streams |
| `tests/` | pytest suite — one file per module |
| `database/` | Persistence layer: `persistence.py` |
| `knowledge/` | Institutional knowledge: `graph_store.py`, `retrieval_engine.py` |

## Adding a New Worker

1. Create `workers/your_worker.py` subclassing `BaseWorker` from `workers/base_worker.py`:
   ```python
   class YourWorker(BaseWorker):
       def _register_handlers(self):
           self._handlers["your_action"] = self._your_action
       def _your_action(self, params):
           return self._gateway.invoke("yourservice.your_tool", "your_action", params)
   ```
2. Register in `workers/mcp_client.py` — add tool mappings to `_TOOL_TO_SERVER` and `_SERVER_TO_TARGET`.
3. Instantiate in `supervisor/agent.py` `SentinalAISupervisor.__init__()` — add to `_worker_factory` and `_WORKER_SERVERS`.
4. Add tests in `tests/test_your_worker.py`.

All tool calls MUST go through `McpGateway.invoke()` — never call backend APIs directly.

## Adding a Synthetic Incident Scenario

Under `eval/scenarios/<scenario_name>/`:
- `alert.json` — raw alert payload (incident summary, service, timestamps)
- `scenario.yml` — metadata: `id`, `type`, `service`, `description`
- `answer.yml` — ground truth: `root_cause`, `confidence_min`, `root_cause_keywords`
- `evidence/` — stub evidence files referenced by the scenario (JSON)

## Running the Benchmark

```bash
python3 -m sentinalbench score eval/scenarios/
# Score a single scenario:
python3 -m sentinalbench score eval/scenarios/my_scenario/
```

Scores are written to stdout and optionally to `eval/results/`.

## Critical Rules

- **Never commit tokens or secrets.** Use env vars or AWS Secrets Manager. `GATEWAY_ACCESS_TOKEN`, `GATEWAY_OAUTH2_CLIENT_SECRET` must not appear in git history.
- **`ITSM_WRITEBACK_ENABLED` defaults false.** The writeback engine in `intelligence/itsm_writebacks.py` is a no-op unless this env var is set to `true`. Never enable in tests.
- **All new features need feature flags.** Gate with env vars defaulting to `false`/`off`. Existing behavior must be unchanged when the flag is not set.
- **All tool calls go through McpGateway.** Workers call `self._gateway.invoke(tool_name, action, params)`. Direct boto3 or HTTP calls bypass auth, rate limiting, dedup, and stub fallback.
- **Circular imports in `supervisor/`.** Do NOT add top-level imports between supervisor modules — use lazy imports inside functions (`from supervisor.foo import bar`).
- **Lazy sklearn imports in `intelligence/`.** sklearn is optional; always guard with `try/except ImportError` at the call site, not at module level.
- **Avoid adding top-level dependencies.** New packages require updates to `requirements.txt` and `Dockerfile`. Use stdlib or existing deps where possible.

## Common Pitfalls

- `McpGateway` is a singleton — call `McpGateway.reset_instance()` in test teardown if you mutate its state.
- `ExecutionBudget` is per-investigation — don't share across test cases.
- `PlannerTrace.stagnation_detected` is only set when `PLANNER_STAGNATION_DETECTION=true`.
- `MCP_DEDUP_ENABLED=true` activates duplicate-call suppression in `McpGateway.invoke()`. Call `gateway.clear_call_signatures()` between test cases.
- Thread-local state in `SentinalAISupervisor._tls` is reset at the start of each `investigate()` call — do not rely on it persisting across calls on the same instance.

## Definition of Done

- [ ] All new tests pass: `python3 -m pytest tests/ -q`
- [ ] Existing test suite unbroken (no regressions)
- [ ] Feature flag added if the change alters runtime behavior (default `false`)
- [ ] No secrets in committed files
- [ ] New workers registered in `McpGateway` and `SentinalAISupervisor`
