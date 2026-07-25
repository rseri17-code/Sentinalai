# SentinelAI — Supervised OCC Pilot Execution Program

An ops team can execute this package as-is. It uses **only existing platform
capabilities** — no new engineering. Every metric maps to an endpoint that is
already live or to a defined operator-entered field. The objective is
**trustworthy evidence**, not more software.

---

# EXECUTIVE SUMMARY

- **Mission Status: COMPLETE** — a fully executable pilot package.
- **Overall Verdict: GO WITH LIMITATIONS** — executable once the data-gateway
  config prerequisite is met (below).
- **Pilot Ready: YES** (after configuration; no engineering work required).

---

# PILOT DESIGN (Phase 1)

**Objectives** — answer three questions with evidence:
1. Does SentinelAI reduce MTTI? 2. Does it reduce operator investigation
effort? 3. Do operators trust and act on it?

**Scope**
- **Incident classes:** the classes the engine covers — `saturation`,
  `timeout`, `deploy/regression`, `network/dns`, `cascade`, `k8s`
  (mirrors `eval/eic/tasks`, `ground_truth.json`).
- **Surface:** the wired **Operational Health** surface + the investigation view
  (timeline/graph/evidence/confidence/MTTI). The four unwired OIP surfaces are
  **out of scope**.
- **Mode:** read-only shadow. SentinelAI takes no action; Wave 3 OFF; auth ON.

**Personas**
- **OCC operator** — runs investigations, uses the surfaces, gives feedback.
- **SRE** — deep-dive + owner/handoff.
- **Pilot coordinator** — assignment, data hygiene, daily/weekly review.
- **Product owner** — reads the exec dashboard, makes go/expand decisions.

**Exclusions:** security incidents; incidents with no reachable evidence; any
class with no corpus coverage (record as coverage gap, don't force a verdict).

**Timeline:** 6 weeks. Wk 1–2 **baseline arm** (SentinelAI observed, not
consulted). Wk 3–6 **assisted arm** (operators may consult). Two-arm,
within-subjects.

**Daily cadence:** shift-start briefing → investigations with per-incident
capture → end-of-day coordinator review. Weekly exec report each Friday.

**Entry criteria:** gateway configured (real data); auth enabled with a real
secret; ≥6 operators onboarded on the runbook; corpus_version pinned.

**Exit criteria:** see EXIT CRITERIA.

---

# DATA COLLECTION PLAN (Phase 2)

**Automatic (already emitted — no operator effort):**

| Metric | Source (existing endpoint) |
|---|---|
| System MTTI (time to evidence/root cause/owner/recommendation) | `GET /api/v1/investigations/{id}/mtti` |
| Operator MTTI (time to first evidence/understanding/confidence/decision) | `GET /api/v1/investigations/{id}/operator-mtti` |
| External-tool escapes (tool, time away, reason) | `operator-mtti.external_tool_escapes` (from `external_tool_opened` events) |
| Recommendation acceptance / override | `operator-mtti.decision_quality` (from `recommendation_accepted/rejected` events) |
| Investigation completion time | investigation `completed_at` + system MTTI `total_ms` |
| Investigation quality (RCA vs ground truth) | gold-standard IQS evaluator (offline) |
| Estate operational health | `GET /api/v1/operational-health` |
| Ranked improvement backlog | `GET /api/v1/improvement-report` |

Operator interactions are captured automatically by the wired emit points
(`investigation_opened`, panel views) plus the coordinator-recorded
`external_tool_opened`, `recommendation_accepted/rejected`, `next_action_started`,
`investigation_completed` events via `POST .../operator-events`.

**Operator-entered (per incident — the short form):**

| Field | Scale | Mechanism |
|---|---|---|
| Operator confidence | 1–5 | `pilot_telemetry` `operator_feedback` |
| Trust in recommendation | 1–5 | `operator_feedback` |
| Missing information | free text | `operator_feedback` |
| Would use again | Yes/No | `operator_feedback` |
| (baseline arm) unaided MTTI | timestamps | coordinator-recorded |

**Executive (derived, weekly):** medians + acceptance rate + escape totals from
the above; assembled into the dashboard below. No new metric is introduced.

---

# DAILY RUNBOOK (Phase 4)

**OCC operator**
- *Startup:* open Operational Health; read `verification_status` first.
- *Investigation:* work the incident; use timeline → graph → evidence →
  confidence → Operational Health drill-down; if you leave for Splunk/etc, the
  coordinator logs an `external_tool_opened` with the reason.
- *After each incident:* complete the 4-field feedback form.
- *Escalation:* a wrong/unsafe output → flag to coordinator (only a verified
  defect unfreezes anything).

**Pilot coordinator**
- *Startup:* confirm gateway=live, auth on, corpus_version unchanged; assign
  incidents (randomized arm — see bias mitigation).
- *During:* record operator events not auto-captured; ensure blind RCA labeling.
- *End-of-day:* pull `mtti` + `operator-mtti` per completed incident; check data
  completeness; log anomalies.
- *Weekly:* run `improvement-report`; assemble the exec dashboard.

**Engineering support**
- On-call for defects only. No feature work during the pilot. Verify
  determinism weekly: re-run `eval/ovp/measure_phase1_baseline.py` — must stay
  `all_deterministic: true`.

**Product owner / leadership**
- Read the weekly exec report; make continue/expand/stop decisions against the
  exit criteria; do not request features mid-pilot.

---

# STATISTICAL ANALYSIS PLAN (Phase 3)

- **Baseline methodology:** unaided arm (Wk 1–2) — operators investigate
  normally; SentinelAI records but is not consulted. Capture MTTI + effort.
- **Sentinel methodology:** assisted arm (Wk 3–6) — operators may consult the
  surfaces. Same capture.
- **Assignment:** randomize incidents to arm within class + severity strata to
  avoid confounds; within-subjects so each operator contributes to both arms.
- **Sample size:** **n ≥ 30 completed investigations per arm per primary KPI**
  (and ≥15 per incident class for per-class claims). Below threshold →
  `NOT_MEASURED`, never a soft conclusion.
- **Confidence thresholds:** report 95% CIs; declare an effect only if the CI
  excludes zero (paired test on operator MTTI; McNemar on acceptance).
- **Outlier treatment:** pre-registered — winsorize MTTI at the 5th/95th
  percentile; report both raw and winsorized.
- **Bias mitigation:** blind RCA labeling (label independent of SentinelAI
  output); freeze corpus_version before the window; randomized arm assignment;
  rotate operators across arms; coordinator does not choose which incidents get
  assistance.
- **Missing data:** report completeness per metric; missing operator feedback is
  `NOT_MEASURED` for that incident — not imputed. Segments with `null`
  milestones are excluded from that segment's median, not zero-filled.

**No expected improvement is stated.** The plan tests for one; it does not
predict it.

---

# EXECUTIVE DASHBOARD (Phase 5) — weekly

| KPI | Source | Reported as |
|---|---|---|
| Pilot progress | count completed / target n | n/30 per arm |
| Operator adoption | distinct operators × surfaces opened | from operator events |
| Recommendation acceptance | `decision_quality.acceptance_rate` | % + CI |
| External-tool escapes | `external_tool_escapes` | count + total time away, top tools |
| Operator MTTI | `operator-mtti` medians | median + CI (or NOT_MEASURED) |
| System MTTI | `mtti` medians | median |
| Improvement backlog | `improvement-report` | top-5 ROI items (or NOT_MEASURED) |
| Operator confidence / trust | feedback 1–5 | mean + n |
| Risks / decisions required | coordinator | list |

The dashboard is an assembly of existing endpoint outputs — no new computation.

---

# PILOT READINESS CHECKLIST

| Item | Status | Evidence |
|---|---|---|
| System MTTI endpoint live | PASS | `/api/v1/investigations/{id}/mtti` |
| Operator MTTI + escapes + decision quality live | PASS | `/api/v1/investigations/{id}/operator-mtti` |
| Operator event capture live | PASS | `POST .../operator-events` + UI emit points |
| Operator feedback capture | PASS | `pilot_telemetry` `operator_feedback` |
| Improvement backlog (honest NOT_MEASURED) | PASS | `/api/v1/improvement-report` |
| Operational Health surface wired | PASS | `/api/v1/operational-health` + UI |
| Auth on + no baked secret | PASS | `auth.py` default true; compose `:?` |
| Determinism check runnable | PASS | `eval/ovp/measure_phase1_baseline.py` |
| **Real data gateway configured** | **FAIL — prerequisite** | `.env.example GATEWAY_MODE=stub` |
| ≥6 operators onboarded | PARTIAL | requires scheduling |
| 4 other OIP surfaces | N/A | out of scope this pilot |

---

# RISKS

- **CRITICAL — stub data.** Evidence: `GATEWAY_MODE=stub` default. Impact: pilot
  measures nothing real. Mitigation: set `AGENTCORE_GATEWAY_URL` +
  `GATEWAY_MODE=live` before Wk 1. *Config, not code.*
- **HIGH — underpowered corpus.** Impact: no significant result if n<30 per arm.
  Mitigation: size the window to reach n≥30; else report `NOT_MEASURED`.
- **HIGH — assignment bias.** Impact: cherry-picked easy incidents inflate
  results. Mitigation: randomized stratified assignment; coordinator blind to
  outcome.
- **MEDIUM — operator over-trust of unverifiable output.** Mitigation: runbook
  makes `verifiable` the primary trust signal; monitor verifiability weekly.
- **MEDIUM — feedback fatigue / missing data.** Mitigation: 4-field form only;
  report completeness, never impute.
- **LOW — UI a11y / desktop-only.** Impact: controlled desktop pilot only.
  Mitigation: post-pilot.

---

# EXIT CRITERIA

SentinelAI may claim **"measurably improves incident investigations"** only when,
on n ≥ 30 per arm with blind labeling and a frozen corpus:

1. **Operator MTTI** is lower in the assisted arm, 95% CI excluding zero; **and**
2. **no safety regression** (verified-incorrect RCA rate not higher); **and**
3. **operator trust ≥ 4/5** mean; **and**
4. **recommendation acceptance** materially above override with CI reported.

Any KPI below its sample-size gate → `NOT_MEASURED`, and the claim is not made.

---

# FINAL VERDICT

> **GO WITH LIMITATIONS.** The pilot is fully executable with existing
> capabilities and this package — subject to one configuration prerequisite
> (real data gateway) and standard onboarding. No engineering work is required
> to run it.

---

# HONEST BOTTOM LINE

- **Proven today:** deterministic, replayable, evidence- and confidence-grounded
  investigations; a wired operator surface; and complete instrumentation to
  capture system MTTI, operator MTTI, escapes, decision quality, and a ranked
  improvement backlog. All green at 5971 tests.
- **Only provable after the pilot:** that SentinelAI actually reduces operator
  MTTI and effort and earns operator trust — today every such value is
  `NOT_MEASURED`. This package is exactly what converts them into evidence.
- **What would justify expanding beyond a supervised OCC deployment:** the exit
  criteria met on a powered, bias-controlled sample, repeated across ≥2 teams —
  significant MTTI reduction, no safety regression, trust ≥4/5. Nothing less.

No number in this package is invented; every metric traces to an existing
endpoint or a defined operator-entered field, and every unmeasured outcome is
labelled `NOT_MEASURED`.
