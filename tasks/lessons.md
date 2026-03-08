# SentinalAI — Lessons Learned

## Lesson 0 — SEED
RULE: Never modify a test assertion to make a test pass. Fix the code under test. If the assertion is genuinely wrong, escalate.

## Lesson 1 — SEED
RULE: Any fix touching the hypothesis scoring path must be followed immediately by running determinism tests before proceeding.

## Lesson 2 — SEED
RULE: AgentCore response fields must never be accessed with dict["key"]. Always use .get("key") or explicit KeyError handling with logging.

## Lesson 3 — SEED
RULE: Do not batch fixes across module boundaries in a single edit. Maximum 3 files per batch. Module boundary = one batch limit.

## Lesson 4 — SEED
RULE: Only modify files in the allowlist (supervisor/, workers/, database/, knowledge/, tests/, tasks/). Never modify Dockerfile, docker-compose.yaml, agentcore_runtime.py, pyproject.toml, or .env.template without explicit user instruction.

## Lesson 5 — SEED
RULE: Do not run pip install. If a scan tool is missing, log it in the Escalation Log and skip that scan.

## Lesson 6 — SEED
RULE: Radon D/E complexity findings are informational only. They do not block exit gates. Log them in the Escalation Log for architectural review.
