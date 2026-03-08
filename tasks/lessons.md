# SentinalAI — Lessons Learned

## Lesson 0 — SEED

CONTEXT:    Test failures during improvement cycles
MISTAKE:    Modifying test assertions to make tests pass
CORRECTION: Always fix the code under test, not the test assertions
RULE:       Never modify a test assertion to make a test pass. Fix the code under test. If the assertion is genuinely wrong, escalate.

## Lesson 1 — SEED

CONTEXT:    Fixes touching hypothesis scoring path
MISTAKE:    Not verifying determinism after scoring changes
CORRECTION: Run determinism tests immediately after any scoring change
RULE:       Any fix touching the hypothesis scoring path must be followed immediately by running tests/test_determinism.py before proceeding.

## Lesson 2 — SEED

CONTEXT:    Parsing AgentCore/boto3 API responses
MISTAKE:    Using dict["key"] access on response fields
CORRECTION: Use .get("key") or explicit KeyError handling with logging
RULE:       AgentCore response fields must never be accessed with dict["key"]. Always use .get("key") or explicit KeyError handling with logging.

## Lesson 3 — SEED

CONTEXT:    Batching fixes across multiple modules
MISTAKE:    Changing too many files at once across module boundaries
CORRECTION: Limit to 3 files per batch, one module boundary per batch
RULE:       Do not batch fixes across module boundaries in a single edit. Maximum 3 files per batch. Module boundary = one batch limit.
