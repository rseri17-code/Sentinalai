# Known Development Workarounds
<!-- Coding-assistant memory — dev environment / CI / test quirks, not production workarounds -->
<!-- Load when: a test is unexpectedly failing, CI behaves differently from local, env setup is needed -->
<!-- Update when: a confirmed workaround unblocks a real development problem -->

## Purpose
Records confirmed workarounds for known development environment quirks in this repo:
- test setup issues
- CI vs local differences
- known pytest / mock limitations
- dependency installation side effects

## Confirmed Workarounds

### Workaround 1 — boto3 installation causes test failures
- **Symptom**: 24 failures in `test_analyzer_branches.py` after `pip install boto3`
- **Root cause**: test fixture mocks only 5 of 9 workers; boto3 lets code paths reach unmocked workers
- **Fix**: In `_make_supervisor_with_data`, set `execute = Mock(side_effect=mock_noop)` on **ALL** workers before overriding specific ones
- **Confirmed**: tasks/lessons.md Lesson 16, 2026-03-09
- **Underlying issue**: test fixture design — not a pytest bug

## Schema for new entries

```
### Workaround N — [date]: [brief name]
- **Symptom**: what the failure looks like
- **Root cause**: known/suspected
- **Fix**: exact steps to unblock
- **Confirmed**: when last verified
- **Underlying issue**: filed? or permanent workaround?
```
