# CODEX GOAL — seri.ai World-Class Upgrade

Paste everything below this line into Codex as the task/goal. It is written to be
deterministic: fixed objective, measurable exit criteria, hard invariants, an exact
per-iteration procedure, and explicit stop conditions.

---

## MISSION

Continuously improve the repository `rseri17-code/seri-ai` (the Next.js site behind
https://seri.ai) until it meets the Definition of Done below. seri.ai is the
definitive public representation of Ravikanth Seri — his experience, engineering
philosophy, technical publications, reference architectures, open-source work, AI
systems, and professional achievements in Operational Intelligence.

Work in strict improvement loops: **inspect → evaluate → prioritize → implement →
validate → commit → repeat**. Never stop after one task; after each validated commit,
re-enter the loop at step 1 until a stop condition is met.

## GROUND TRUTH (do not rediscover; do not contradict)

- Stack: Next.js 15 App Router, TypeScript, Tailwind, Framer Motion, Supabase
  pgvector, Anthropic/OpenAI SDK, Vercel. Node >= 20.18.
- Quality ledger: `WORLD_CLASS_SCORECARD.md` (24 dimensions, evidence-based scores).
- Validation harness: `npm test` (all `validate:*` scripts + evals + typecheck) and
  `npm run build`. These are the only definitions of "passing".
- Ask Ravi already exists at `/ask` (`app/api/ask/route.ts`): safety refusal, rate
  limit, local-search fallback, pgvector retrieval, model synthesis, 105 fixtures.
- Portrait governance: `content/portrait-intake.json` — target asset
  `/identity/ravikanth-seri-portrait.webp`, ≥800×800 source, ≤250 KB optimized,
  plain alt text, non-photographic mark remains the fallback.
- Benchmarks: karpathy.ai (artifact density, subtraction, instant load) and
  andrewng.org + avatar.andrewng.org (audience funnels, portrait-led identity,
  persona-grounded AI assistant with memory). See `SITE_BENCHMARK_REVIEW.md`.

## DEFINITION OF DONE (all must hold simultaneously)

1. Every dimension in `WORLD_CLASS_SCORECARD.md` scores ≥ 9.0 **with cited evidence
   in the same row** (a score without new evidence is invalid and must be reverted).
2. `npm test` and `npm run build` pass from a clean checkout.
3. The approved portrait renders on the home, background, and resume surfaces and
   passes the portrait-intake validator.
4. Ask Ravi meets the avatar-grade bar defined below and its eval suite passes with
   ≥ 120 fixtures including ≥ 10 adversarial/prompt-injection cases.
5. A first-time visitor path (home → signature artifact → Ask) is reachable in ≤ 3
   clicks from `/`, verified by the route validators.
6. Lighthouse-class budgets hold: first-load JS ≤ 110 kB on `/`, ≤ 210 kB on
   `/investigation-room`; no route regresses its current budget.
7. No new self-audit/report documents exist that are not required by a validator
   (see Invariant I6).

## INVARIANTS (never violate; a violated invariant = revert the change)

- I1 **Public safety**: never publish employer-specific product names, internal
  systems, confidential screenshots, logs, or proprietary architecture. The
  public-safety scanner must pass on every commit.
- I2 **No fabricated evidence**: never invent metrics, reviews, testimonials,
  benchmark numbers, or user quotes. `NOT_MEASURED` stays visible until measured.
- I3 **Determinism**: every behavioral claim in content must be backed by a fixture,
  validator, or linked artifact. New features ship with their validator/eval in the
  same commit.
- I4 **Surgical diffs**: one prioritized improvement per commit; do not refactor
  unrelated code; do not reformat untouched files.
- I5 **Portrait provenance**: use only the image file the owner places in the repo
  or provides directly. NEVER scrape LinkedIn or any third-party site for the
  photo, and never substitute an AI-generated likeness. If no approved image file
  exists in the repo, skip portrait work and record it as blocked-on-human.
- I6 **Meta-ratio**: do not create new audit/report/scorecard documents. Update the
  existing scorecard only. Every iteration must change something a visitor sees or
  uses; documentation-only iterations are forbidden unless fixing factual errors.
- I7 **No secrets**: no keys, tokens, or `.env` values in code, content, or commits.
- I8 **Identity truth**: the assistant always self-identifies as an AI over
  Ravikanth's approved public work, never as Ravikanth himself.

## PER-ITERATION PROCEDURE (execute exactly, in order)

1. **Inspect**: read `WORLD_CLASS_SCORECARD.md` and the output of `npm test`.
   List the 3 lowest-scoring dimensions whose next-proof step is executable inside
   the repository without human input.
2. **Prioritize**: select exactly one item using this fixed priority order:
   P0 broken build/test → P1 Definition-of-Done items 3–5 not yet met →
   P2 lowest scorecard dimension with an executable next proof →
   P3 benchmark-gap items from `SITE_BENCHMARK_REVIEW.md` §2.
   Ties break toward the item that most improves what a first-time visitor
   experiences in their first 10 minutes.
3. **Declare**: before coding, write one sentence: "This iteration changes X so
   that a visitor can Y, verified by Z." If Z is not an existing or new automated
   check, choose a different item.
4. **Implement** the smallest change that satisfies the declaration.
5. **Validate**: run `npm test` then `npm run build`. Both must pass. If either
   fails, fix forward or revert; never commit red.
6. **Record**: update the affected scorecard row(s) — score may only move when the
   Evidence cell cites the new artifact/validator. Append one line to
   `changelog` content if the change is visitor-visible.
7. **Commit** with message `improve(<area>): <what> — proof: <validator/fixture>`
   and push to the designated branch.
8. Return to step 1.

## WORKSTREAM SPECS

### A. Portrait (blocked-on-human until the file exists)
Preconditions: owner has saved their own LinkedIn profile photo (they own it) or
another approved photo into the repo at `incoming/portrait-source.(jpg|png)`.
Then, deterministically:
1. Validate source ≥ 800×800, no employer branding/badges/screens in frame.
2. Generate `/public/identity/ravikanth-seri-portrait.webp` (≤ 250 KB) and `.jpg`
   fallback; alt text exactly "Portrait of Ravikanth Seri".
3. Wire into home identity block, `/background`, `/resume`; keep the mark as
   fallback when the file is absent.
4. Delete `incoming/portrait-source.*`; update `content/portrait-intake.json`
   status to `integrated`; run full validation.

### B. Ask Ravi → avatar-grade ("Ask Seri" bar, modeled on avatar.andrewng.org)
Ship in this order, one iteration each, only if all validators stay green:
1. **Persona grounding**: system prompt derived from `content/builder-dna.json`,
   principles, and doctrine so answers carry Ravikanth's engineering voice;
   add ≥ 10 fixtures asserting voice + citation presence (I8 applies).
2. **Session continuity (client-side first)**: persist conversation history in
   `localStorage`, restore on return, offer "continue where you left off". No
   server-side PII storage without explicit owner sign-off (record as
   blocked-on-human if attempted).
3. **Suggested-follow-up engine**: each answer returns 3 grounded next questions
   from the knowledge graph; fixture-tested.
4. **Answer quality gate**: extend `validate:ask-quality` so every fixture answer
   must include ≥ 1 citation to a live route; broken-citation = failing test.
5. **Voice (optional, last)**: only via a keyless browser API (Web Speech) —
   no new paid vendor without owner sign-off.

### C. Site-as-first-case-study (SRE evidence)
1. Add a public `/status`-style page rendering build-time evidence: validator
   counts, budgets, fixture counts, last-build timestamp — all generated from the
   real harness output, never hand-written (I2).
2. When production analytics exist, surface privacy-safe uptime/latency/fallback
   metrics there; until then the page must label them `NOT_MEASURED`.

### D. Subtraction pass (Karpathy lesson)
Identify routes with no inbound links from the first-visit path and no search
retrieval coverage; propose merges/redirects in the iteration declaration; a
subtraction only ships when route + link validators stay green and the sitemap
shrinks or holds.

## BLOCKED-ON-HUMAN LEDGER

When an item needs the owner (portrait source file, Supabase production ingestion,
external reviewers, paid voice vendor, deployment credentials), append one line to
`BLOCKED_ON_HUMAN.md` (create once; this file is exempt from I6) with: item, why
blocked, exact artifact needed. Never work around a block by fabricating (I2) or
scraping (I5).

## STOP CONDITIONS

Stop and report (do not keep looping) when any of these holds:
- All Definition-of-Done items pass → final report of evidence per item.
- Every remaining scorecard gap is blocked-on-human → report the ledger.
- The same validator fails 3 consecutive fix attempts → report the failure verbatim.
- A change would require violating any invariant → report which and why.
