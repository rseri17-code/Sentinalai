# EFIC — Enterprise Failure Intelligence Corpus

Knowledge, not framework. EFIC is the canonical, deterministic knowledge base of
realistic enterprise failure modes used to answer one question: **can SentinelAI
determine the correct root cause from the evidence its connected MCPs expose?**

It **reuses** existing assets — the EIC task format (`sentinel_core.eic.make_task`,
so the EB-0 runner grades EFIC unchanged) and the `eval/enterprise` builder
pattern. It adds **no framework**.

## What a scenario is
Each corpus entry = `{task, expected, efic}`:
- **`task`** — an EIC-compatible task: incident + cross-MCP telemetry + the
  **hidden** ground truth and traps. The engine must **earn** the root cause; it
  never receives the answer.
- **`expected`** — operator-facing: owner, confidence range, recommendation.
- **`efic`** — the enterprise scenario model: failure family + mode, business
  impact, **MCP utilization** (`required` / `optional` / `expected_empty` /
  `not_applicable` for every MCP), contributing factors, **negative evidence**,
  **red herrings**, hypotheses considered/eliminated, reasoning category,
  difficulty, and a content-addressed **replay seed** (the task hash).

## Design rules
- **Distinct reasoning per scenario** — deduplicated by
  `(family, mode, reasoning_category)`; diversity over count.
- **Cross-MCP** — every scenario requires ≥2 MCPs and a genuine correlation; an
  MCP appears only if it contributes investigation value.
- **The engine must earn it** — ground truth + traps are hidden; every scenario
  carries negative evidence (what *rules out* a wrong hypothesis) and a red
  herring (misleading-but-benign signal).
- **Deterministic** — content-addressed task hashes, no clock, no randomness;
  rebuild is byte-identical (guarded by a test).

## Coverage (honest)
`coverage.json` records exactly which taxonomy modes have scenarios and which are
**gaps** — the foundational set is intentionally not complete, and gaps are
reported, never padded. Current foundational set: **16 scenarios across 11 of 14
families**, each a distinct reasoning problem; **28 taxonomy modes remain gaps**
(the next-highest-value work).

## Files
| File | Purpose |
|---|---|
| `build_corpus.py` | taxonomy + scenarios (deterministic builder) |
| `taxonomy.json` | canonical family → failure-mode taxonomy |
| `corpus.json` | the materialized scenarios (committed; guarded vs the builder) |
| `coverage.json` | family / mode / MCP / reasoning / negative-evidence / red-herring coverage + gaps |

## Usage
```bash
python3 eval/efic/build_corpus.py            # (re)materialize the corpus + coverage
python -m enterprisebench run --corpus eval/efic/corpus.json   # EB-0 runs it
```
Without engine submissions every scenario is `NOT_MEASURED` (the corpus→engine
injection is EB-2 `BenchMCPSource`, not built). EFIC measures **whether the
knowledge base poses well-formed, distinct, cross-MCP reasoning problems** — it
does **not** prove real operator outcomes (those remain `NOT_MEASURED` until the
supervised pilot).

Tests: `tests/efic/test_efic_corpus.py` (14).
