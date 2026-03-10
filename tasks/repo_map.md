# SentinalAI — Repository Map

## Architecture
```
Incident In → [Fetch] → [Classify] → [Playbook] → [Multi-Hypothesis Score]
→ [Evidence-Weighted RCA] → Structured Result Out
```

## Core Modules

### supervisor/ — Investigation pipeline
| File | Lines | Purpose |
|------|-------|---------|
| agent.py | ~2198 | Full pipeline + hypothesis engine + compute_confidence |
| tool_selector.py | ~583 | Classifier (keyword) + 10 playbooks + LLM fallback |
| guardrails.py | ~76 | Budget, circuit breaker, query validation |
| observability.py | ~111 | OTEL spans + GenAI semantic conventions |
| llm.py | ~94 | Bedrock Converse (additive, non-blocking) |
| llm_judge.py | ~72 | LLM-as-judge quality scoring |
| memory.py | ~140 | AgentCore Memory STM + LTM |
| eval_metrics.py | ~163 | 20+ OTEL metric instruments |
| confidence_calibrator.py | ~182 | Calibration map for confidence scores |
| incident_model.py | ~101 | Incident data model |
| intake.py | ~145 | Incident intake/parsing |
| rca_report.py | ~125 | RCA report generation |
| receipt.py | ~91 | Receipt collector for audit trail |
| remediation.py | ~103 | Remediation template engine |
| replay.py | ~48 | Replay store for determinism verification |
| severity.py | ~100 | Severity detection |
| ground_truth_eval.py | ~144 | Ground truth evaluation |

### workers/ — Action dispatch
| File | Lines | Purpose |
|------|-------|---------|
| mcp_client.py | ~1056 | AgentCore gateway + OAuth2 + rate limiting |
| base_worker.py | ~34 | Base action dispatch + timing |
| ops_worker.py | ~14 | Moogsoft integration |
| log_worker.py | ~23 | Splunk integration |
| metrics_worker.py | ~15 | Sysdig integration |
| apm_worker.py | ~21 | Dynatrace + SignalFx |
| knowledge_worker.py | ~38 | AgentCore Memory |
| itsm_worker.py | ~32 | ServiceNow |
| devops_worker.py | ~36 | GitHub |

### knowledge/ — Knowledge layer
| File | Purpose |
|------|---------|
| retrieval_engine.py | Similarity retrieval + confidence boost |
| graph_store.py | Knowledge graph store |
| graph_backend_json.py | JSON-backed graph backend |
| metadata_filter.py | Metadata filtering |

### database/ — Persistence
| File | Purpose |
|------|---------|
| connection.py | PostgreSQL + pgvector |
| persistence.py | Data persistence layer |

### tests/ — Test suite
- 49+ test files, 1707+ tests
- 96.29% coverage
- Key test files: test_determinism.py, test_scoring_purity.py

### CI
- .github/workflows/ci.yml — 9 jobs + ci-gate
- .github/workflows/pr.yml — per-PR quality check
- .github/workflows/nightly.yml — 2am CVE + drift + determinism

## Environment
- Python: 3.11.14
- Coverage floor: 80% (pyproject.toml)
- Test timeout: 120s
- Spec version: 5.0
