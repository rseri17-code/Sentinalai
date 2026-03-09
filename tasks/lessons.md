# SentinalAI — Lessons Learned

## Lesson 0 — SEED
RULE: Never modify a test assertion to make a test pass. Fix the code under test. If the assertion is genuinely wrong, escalate.

## Lesson 1 — SEED
RULE: Scoring path fix: run test_determinism.py BEFORE and AFTER. Both must pass.

## Lesson 2 — SEED
RULE: AgentCore responses: always .get("key"), never ["key"].

## Lesson 3 — SEED
RULE: Never batch fixes across module boundaries. Max 3 files per commit.

## Lesson 4 — SEED
RULE: CI fails, local passes → check Python version and env vars first.

## Lesson 5 — SEED
RULE: Probe that doesn't reproduce = wrong hypothesis. Rethink before fixing.

## Lesson 6 — SEED
RULE: Check tasks/patterns.md before forming any hypothesis.

## Lesson 7 — SEED
RULE: Check awesome-claude-code before building any non-trivial capability from scratch.

## Lesson 8 — SEED
RULE: Three hypotheses minimum on HIGH+ issues.

## Lesson 9 — SEED
RULE: Write the regression test BEFORE the fix. Confirm it fails first.

## Lesson 10 — SEED
RULE: claude-mem save after EVERY resolved issue, not just session end.

## Lesson 11 — SEED
RULE: SPEC MODE required for agent.py or mcp_client.py — both >1000 lines.

## Lesson 12 — SEED
RULE: LLM refinement path must never block the deterministic path.

## Lesson 13 — SEED
RULE: boto3.Session() is per-investigation. Never cache at module or class level.

## Lesson 14 — SEED
RULE: Radon will flag agent.py and mcp_client.py. Fix by extracting at natural phase boundaries — FETCH, CLASSIFY, PLAYBOOK, ANALYZE are the lines.

## Lesson 15 — SEED
RULE: test_determinism.py and test_scoring_purity.py must exist before any scoring or classifier work begins. Create them if missing.

## Lesson 16 — 2026-03-09
CONTEXT: Installing boto3 caused 24 test failures in test_analyzer_branches.py.
MISTAKE: Assumed installing a declared dependency (boto3) would be safe. The test fixture only mocked 5 of 9 workers, and with boto3 present, code paths reached the unmocked workers.
CORRECTION: After installing any dependency, run the full test suite immediately. Fix test fixtures before proceeding.
RULE: When installing a new dependency, run the full suite before any other work. Mock fixtures must cover ALL workers with at least a noop, not just the ones under test.
