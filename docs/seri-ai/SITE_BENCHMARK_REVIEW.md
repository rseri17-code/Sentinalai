# seri.ai Benchmark Review — karpathy.ai vs andrewng.org vs seri.ai

Reviewer lens: Senior SRE / AIOps Distinguished Engineer (NVIDIA/Google calibre bar).
Date: 2026-08-24. Sources: seri-ai repository at commit `64d3330`, public knowledge of
karpathy.ai and andrewng.org (direct fetch blocked from this environment; verified via
search — see References).

## 1. What each site optimizes for

### karpathy.ai — credibility through subtraction
- Essentially a single page: short bio, photo, and links out to the actual work —
  GitHub repos, papers, YouTube lectures, blog posts.
- Near-zero chrome: no framework-heavy UI, instant first paint, no navigation maze.
- The site makes almost no claims about Karpathy; the artifacts (nanoGPT, CS231n,
  Zero-to-Hero lectures) carry all the authority. The site is a **pointer, not a pitch**.
- Lesson: a world-class engineer's site earns trust by the density of *usable public
  artifacts per click*, not by describing its own quality.

### andrewng.org — an audience funnel plus an AI engagement layer
- Clean multi-page hub: courses, publications, The Batch newsletter, about/bio — each
  page is a funnel to a concrete next action (enroll, subscribe, read).
- Strong human identity: a professional portrait is central to the brand.
- **AI Andrew** (avatar.andrewng.org, built with RealAvatar + DeepLearning.AI): a
  persona-grounded assistant trained on his teaching and writing, available as text
  chat and voice, with cross-session memory ("remembers your story, picks up where
  conversations left off"). Positioned as mentorship, not a FAQ bot.
- Lesson: the avatar works because it is grounded in a *large public corpus* (courses,
  letters, talks) and offers a relationship (memory, follow-ups), not one-shot answers.

## 2. Where seri.ai stands today (evidence from the repo)

Strengths — genuinely ahead of both benchmarks:
- **Engineering governance**: ~25 deterministic validators (content, coherence,
  contracts, routes, links, a11y, security, performance, ask-quality) gate every build.
  Neither benchmark site has anything like this.
- **Ask Ravi already exists** (`/ask` + `/api/ask`): public-safety refusal, rate
  limiting, timeouts, local-search fallback → pgvector retrieval → model synthesis,
  answer metadata, citations, 105 deterministic fixtures. This is architecturally more
  rigorous than most production chatbots.
- **Reliability posture**: runbook, budgets (107–203 kB first-load JS), rendered-route
  validation, prompt-injection fixtures, privacy-safe analytics.

Gaps — where both benchmarks beat seri.ai:

| Gap | Evidence | Benchmark contrast |
| --- | --- | --- |
| **No human face** | Portrait intake contract exists but `status: waiting_for_approved_source_image`; identity is an abstract mark. | Both Ng and Karpathy lead with a real photo. Trust in a *person* needs a person. |
| **Meta-documentation inversion** | 8+ self-audit reports at repo root; scorecard tracks 24 dimensions of the site describing itself. The ratio of "evidence about the site" to "artifacts a visitor can use" is inverted vs Karpathy. | Karpathy publishes ~zero process documents and ~dozens of runnable artifacts. |
| **Memorability 7.9 (own scorecard)** | 40+ routes dilute the first 10 minutes; no single signature artifact dominates. | Karpathy = nanoGPT/lectures; Ng = courses/avatar. One thing per visit. |
| **No external validation** | Scorecard repeatedly notes "no independent review has run"; RCA gold set n=3. | Both benchmarks are validated by millions of external users. |
| **Ask is one-shot** | No cross-session memory, no voice, no proactive follow-up. | AI Andrew remembers users and continues conversations. |
| **No live production telemetry** | Uptime, latency, fallback-rate all `NOT_MEASURED`. | An SRE's portfolio site with no published SLO evidence undercuts the thesis. |

## 3. Distinguished-engineer verdict

seri.ai is an 8.x site with 10/10 process and 7/10 product. The failure mode to avoid
is polishing the harness instead of the experience. The path to 10/10 is:

1. **Put the human in it** — approved portrait wired through the existing intake contract.
2. **Invert the meta ratio** — every iteration must ship something a visitor uses
   (artifact, demo, answer quality), not another report about the site.
3. **Make Ask Ravi the signature surface** — avatar-grade: persona-grounded, cited,
   memorable, eventually voice; it is the one feature neither Karpathy nor a generic
   portfolio has, and Ng has proven the pattern.
4. **Publish live SRE evidence** — real uptime/latency/fallback dashboards for the
   site itself. For an Operational Intelligence thesis, the site must be its own
   first case study: *operate the site the way the doctrine says operations should run*.
5. **Ruthless subtraction** — collapse or de-emphasize routes that don't serve the
   first-visit narrative (Karpathy's lesson).

## References

- AI Andrew overview: https://avatar.andrewng.org/ and https://www.andrewng.org/ai-andrew
- RealAvatar (builder of AI Andrew): https://www.realavatar.ai/
- Andrew Ng announcement: https://x.com/AndrewYNg/status/1879590674561110219
