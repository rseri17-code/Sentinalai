# SentinalAI — Claude Code Native Memory

Three-tier memory system. Hooks write hot memory automatically.
Agent writes warm memory during investigation. Cold is archive only.

## Tiers

### Hot (`memory/hot/`) — load every session
Auto-generated or actively maintained. Small. Always current.

| File | Writer | Purpose |
|---|---|---|
| `session_state.md` | SessionStart hook | Branch, commits, modified files, PROMOTE alerts |
| `active_task.md` | PreCompact hook | Current objective, changed files, next action |
| `current_decisions.md` | Agent (during session) | Decisions made this session — harvested by SessionEnd |
| `stop_log.md` | SessionEnd hook | Append-only reflection log + promotion queue |

### Warm (`memory/warm/`) — load by task type
Agent loads the relevant file(s) when the task demands it.
Never load all warm files in one session.

| File | Load when |
|---|---|
| `rca_patterns.md` | Incident investigation, hypothesis scoring |
| `topology_substrate.md` | Blast radius, upstream/downstream analysis |
| `splunk_query_library.md` | Log retrieval, Splunk-based evidence gathering |
| `operational_decision_ledger.md` | Playbook selection, budget decisions, scoring changes |
| `known_workarounds.md` | Blocked investigation, known infra quirks |
| `recurring_mistakes.md` | Before any change to agent.py, mcp_client.py, scoring |

### Cold (`memory/cold/`) — load only if explicitly requested
Archive. Never load automatically.

| File | Content |
|---|---|
| `archived_decisions.md` | Superseded decisions from operational_decision_ledger |
| `stale_patterns.md` | Patterns no longer seen or superseded |
| `deprecated_approaches.md` | Approaches tried and rejected with reason |

## Promotion Rules

Mark a `stop_log.md` entry with `[PROMOTE: <target>]` to flag it for promotion.

Valid targets: `rca_patterns`, `topology_substrate`, `splunk_query_library`,
`operational_decision_ledger`, `known_workarounds`, `recurring_mistakes`,
`lessons` (→ tasks/lessons.md), `decisions` (→ tasks/decisions.md)

**Promotion requires one of:**
- Explicit `[PROMOTE: target]` marker written by the agent
- The same pattern appearing in 2+ stop_log entries
- A confirmed, deterministically-validated finding

**Never promote:**
- One-off debugging noise
- Unverified hypotheses
- Speculative patterns

SessionStart will surface pending PROMOTE markers at the top of its output.
