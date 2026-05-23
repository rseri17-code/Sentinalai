# Splunk Integration Patterns
<!-- Coding-assistant memory — NOT a query library for live Splunk searches -->
<!-- Load when: modifying log_worker.py, adding new log search actions, debugging Splunk integration code -->
<!-- Update when: a code pattern is confirmed to work correctly with the Splunk integration -->

## Purpose
Records how the Splunk integration is implemented in this codebase — patterns
in `workers/log_worker.py`, how actions are dispatched via `mcp_client.py`,
and common mistakes when adding new log retrieval actions.

## Integration Architecture

```
log_worker.py
  └── inherits from base_worker.py
  └── actions: search_logs, get_error_logs, get_change_data
  └── all calls routed through workers/mcp_client.py → AgentCore → Splunk
  └── response fields: always use .get("key") — never ["key"]

Action dispatch pattern:
  base_worker.execute(action, params)
    → mcp_client.call_tool(worker_name, action, params)
      → AgentCore OAuth2 + rate limiting
        → Splunk tool

Budget impact: each log_worker action counts against INVESTIGATION_BUDGET_MAX_CALLS (20)
```

## Common Mistakes When Modifying log_worker.py

- Using `response["field"]` instead of `response.get("field")` → KeyError on partial results
- Not mocking the new action in test fixtures → 24+ test failures (see tasks/lessons.md Lesson 16)
- Adding an action without updating `_stub_response` dispatch table in mcp_client.py

## Schema for new entries

```
### Pattern N — [date]: [brief name]
- **Action**: the log_worker action this applies to
- **Pattern**: correct implementation approach
- **Confirmed by**: test or manual verification
```

## Confirmed Patterns

_None added yet beyond common mistakes above._
_Promote from stop_log.md using [PROMOTE: splunk_query_library]._
