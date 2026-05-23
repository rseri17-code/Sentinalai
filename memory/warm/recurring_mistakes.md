# Recurring Implementation Mistakes
<!-- Coding-assistant memory — patterns of mistakes made 2+ times across coding sessions -->
<!-- Load when: before modifying agent.py, mcp_client.py, scoring, or CI workflows -->
<!-- Update when: the same class of mistake appears in 2+ sessions -->

## Purpose
High-recurrence filter on tasks/lessons.md. Only enters here if the same
class of mistake has occurred more than once. Single-occurrence mistakes
stay in tasks/lessons.md only.

## Recurring Mistakes

### Mistake Class 1 — AgentCore response field access
- **Pattern**: using `response["key"]` instead of `response.get("key")`
- **Trigger**: modifying any worker or mcp_client.py under time pressure
- **Prevention**: grep the diff for `\["` patterns before committing worker code
- **Occurrences**: documented in tasks/lessons.md Lesson 2 (seed), confirmed as common
- **Test catch**: no specific test — fails at runtime on partial API responses

### Mistake Class 2 — Test fixture incomplete worker mocking
- **Pattern**: adding a new worker action without updating test fixtures to mock it
- **Trigger**: adding new capability to any worker file
- **Prevention**: after adding a worker action, search for `_make_supervisor` in tests and add the action to all mock setups
- **Occurrences**: tasks/lessons.md Lesson 16 (boto3 install, 2026-03-09)
- **Test catch**: `test_analyzer_branches.py` fails with JSON serialization error on MagicMock

## Schema for new entries

```
### Mistake Class N — [first seen]: [brief name]
- **Pattern**: what the mistake looks like in code or test output
- **Trigger**: what coding context causes it
- **Prevention**: specific check or habit
- **Occurrences**: [date1], [date2], ...
- **Test catch**: which test surfaces it (or "no test — runtime only")
```
