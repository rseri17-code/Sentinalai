# SentinelAI — Operator Runbook (Supervised Pilot)

**Read-only decision support. SentinelAI takes no action and holds no authority.**
It reads completed investigations and tells you *what to look at, why, and whether you can
trust it*. You decide. During the pilot, use it alongside your normal workflow — never in place
of your judgment.

Every recommendation carries the **incident ids** that support it, and a **verifiable** flag
(backed by an R1 corpus stamp). If something isn't verifiable, treat it as a lead, not a fact.

---

## The five surfaces — when to use each

| Surface | Use it when you ask… | Key fields |
|---|---|---|
| **Daily Operations Brief** | "I just came on shift — what matters?" | `critical_services`, `applications_at_risk`, `highest_priority_actions`, `verification_status` |
| **Operational Health** | "Which service is worst right now, and why?" | `attention_order`, per-service `health_band`, `why`, `next_action` |
| **Service Reliability** | "Is this service reliable — improving or degrading?" | `reliability_band`, `reliability_direction`, `fix_first`, `affecting_incidents` |
| **Incident Trends** | "Is this recurring? Getting worse?" | `what_is_increasing`, `what_is_recurring`, `changed_since_previous` |
| **Application Health** | "Is my app at risk; who owns the driver?" | `health_band`, `owner`, `driving_incidents`, `next_action` |

---

## By role

### OCC operator — start of shift
1. Open the **Daily Operations Brief**.
2. Read `verification_status` first. If `verifiable: true`, the brief's conclusions are
   corpus-backed. If not, escalate cautiously.
3. Work the top of `highest_priority_actions`. Each item names a `target` and its `evidence`
   (incident ids) — open those incidents to confirm before acting.
4. Note `critical_services` and `applications_at_risk` for handoff.

### SRE — triage and deep dive
1. From the brief or **Operational Health** `attention_order`, pick the worst service.
2. Open **Service Reliability** for it. `reliability_direction` tells you the trajectory:
   - `degrading` → prioritise; check `fix_first`.
   - `improving` → likely already being handled.
   - `insufficient_history` → not enough periods yet; use judgment.
3. Cross-check **Incident Trends** `what_is_recurring` — if this cause recurs, it deserves a
   durable fix, not another one-off.
4. Confirm with the `affecting_incidents` before you act.

### Application owner — ownership and handoff
1. Open **Application Health** for your application.
2. `health_band` + `why` gives the one-line status; `driving_incidents` are the incidents
   pulling it down.
3. `owner` is read from existing incident metadata — use it to route, and correct it in the
   source system if wrong (SentinelAI only reflects what's recorded).
4. `next_action` is the suggested first move; `verifiable` tells you whether to trust it outright.

---

## Reading confidence and evidence
- **`confidence`** — the evidence-derived confidence already computed by the investigation.
  It is *not* a promise; corroborate with the cited incidents.
- **`evidence`** — `used` / `unavailable` counts. High `unavailable` means the picture is
  partial; weight the recommendation accordingly.
- **`verifiable` / `verification_status`** — whether every conclusion is reproducible from the
  frozen corpus. This is the single most important trust signal.

## What SentinelAI will NOT do
- It will not act, page, remediate, or gate incident response.
- It will not invent evidence — if a source was unavailable, it says so.
- It will not hide uncertainty — `insufficient_history`, `unavailable`, and `(unresolved)`
  are shown, not smoothed over.

## During the pilot
- Use the surfaces naturally; a facilitator records timing and whether you followed each
  recommendation (out-of-band — the platform is unchanged).
- After each incident, complete the short feedback form (usefulness, clarity, trust, missing
  info, confusing outputs, workflow fit, cognitive load).
- If an output looks wrong, **flag it** — a verified defect is the only thing that unfreezes
  the architecture during the pilot.
