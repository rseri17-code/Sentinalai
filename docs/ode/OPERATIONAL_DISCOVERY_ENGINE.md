# Operational Discovery Engine (ODE)
**Offline operational knowledge discovery — produce-only, deterministic, append-only.**
Package: `sentinel_core/ode/`. A research track that runs in parallel with the production
roadmap; it does **not** gate Wave-3 readiness (that stays driven by the shadow pilot,
Gold Dataset, and EIC).

> Not prediction. Not anomaly detection. Not another RCA engine. ODE mines the *history*
> of completed investigations and **discovers previously-unknown operational relationships**.
> It answers: "what operational knowledge exists today that did not exist six months ago?"

## Scope discipline
Composes existing investigation outputs into observations, then mines *across* them. Changes
no runtime path, no authority, no Wave 3; touches neither IQS, EIC, nor the Gold Dataset.
Deterministic (no clock — times come from incident data; no randomness — seeded bootstrap),
append-only, replayable, removable.

## Observation (`observation`)
One completed investigation → one observation: incident_type, service, incident_time
(from the incident, never wall-clock), affected_services, causal_edges (root→symptom +
chain), decisive_evidence, winner, ruled_out (false leads), operator_interventions,
outcome_correct.

## Discovery types & miners
| Type | Miner | Discovers |
|---|---|---|
| **topology** | `mine_topology` | recurring causal edges **not in the declared CMDB** — hidden dependencies |
| **temporal** | `mine_temporal` | recurring ordered incidents (service A precedes B within a window) + median lead time |
| **evidence** | `mine_evidence` | evidence that consistently becomes **decisive** for an incident class |
| **hypothesis** | `mine_hypothesis` | recurring **false leads** per class |
| **operational** | `mine_operational` | **latent failure clusters** — services that recurrently fail together |
| **human** | `mine_human` | operator interventions that consistently precede a **correct** outcome |

## Discovery Record (deterministic)
`discovery_id` (sha256 of type+signature), discovery_type, description, signature,
supporting_investigations, contradicting_investigations, affected_services,
recurrence_count, first_observed, last_observed, confidence, **statistical_support**
(opportunities, recurrence, support_rate, **ci95**, contradictions, underpowered,
significant), reproducibility (split-half). A pattern is only a discovery if it recurs
≥ `_MIN_RECURRENCE` in ≥ `_MIN_SUPPORT` of its opportunities over ≥ `_MIN_OBS_FOR_CLASS`
observations — otherwise it is silently not emitted (no hallucinated knowledge).

## Discovery Quality Score (DQS)
`discovery_quality_score`: weighted composite of **novelty** (0 if the signature is already
known), **recurrence**, **reproducibility**, **operational usefulness** (per type),
**confidence**, **stability** (1 − contradiction rate). Topology/temporal discoveries carry
the highest usefulness weight (they encode knowledge the CMDB lacks).

## Longitudinal tracking (`longitudinal_update`)
Compares two discovery sets across time: **strengthened** (confidence up), **weakened**
(down), **disproven** (contradictions > recurrence), **retired** (no longer present),
**new**. This is how the knowledge base evolves — and how a discovery can be revoked when
the evidence turns against it.

## Demonstration (`eval/ode/discoveries.json`)
On a synthetic 6-incident saturation history (declared deps = checkout→api), ODE discovers:
- **topology (DQS 1.0):** undeclared dependency `db failure propagates to checkout` — a
  relationship the CMDB did not encode.
- **operational:** latent failure cluster `checkout + db`.
- **evidence:** `db_pool_metrics` is consistently decisive for saturation.
- **hypothesis:** `dns failure` and `bad deploy` are recurring false leads.
No **temporal** discovery fires when incidents are a day apart (outside the 2h window) — the
engine does not invent lead-time relationships it cannot support.

## Honest corpus limitation
Discovery requires *history*. The current 3-incident ground-truth corpus is below the
recurrence floor, so ODE would emit **zero** discoveries on it — correctly. It becomes
valuable only as the shadow pilot accumulates a real multi-incident corpus; the
demonstration above uses synthetic history to prove the miners work end-to-end.

## Enterprise value
This is the first program since Tranche 5 that expands SentinelAI's long-term value rather
than its evaluation surface: investigations stop being one-shot explanations and start
**generating reusable operational knowledge** — hidden dependencies, decisive-evidence
shortcuts, recurring false leads, failure clusters. Over time, ODE is how SentinelAI turns
"we investigated 10,000 incidents" into "here is what we now know about this estate that no
CMDB or runbook records."

## Guarantees
Deterministic (double-run byte-identical), replayable (deterministic ids + seeded
bootstrap), append-only, removable (delete `sentinel_core/ode/` + tests + `eval/ode/`),
fully regression-tested. No runtime, authority, Wave 3, retrieval, IQS, EIC, or Gold Dataset
was touched.
