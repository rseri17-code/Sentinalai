# SentinelAI — Pilot Deployment Checklist

Operational-readiness gate for the supervised pilot. Read-only shadow deployment: SentinelAI
runs alongside human incident response with **no authority** and Wave 3 **OFF**. Every item is
a checkbox the pilot owner signs off before start.

---

## 1. Configuration
- [ ] Wave 3 disabled (no runtime retrieval authority); runtime remains advisory-only.
- [ ] Frozen Corpus pinned: a single `corpus_version` captured **before** the pilot window and
      recorded in the pilot log. Learning may continue for future runs; the pilot reads the
      pinned corpus (R1 hermetic replay).
- [ ] OIP services confirmed present and importable: `operational_health`, `incident_trends`,
      `application_health`, `service_reliability`, `daily_operations_brief`.
- [ ] Incident metadata mapping verified: `service`, `application`, `owner`/`team`, timestamp
      fields populated for the pilot corpus (these drive grouping, ownership, and periods).
- [ ] No new engines, scoring, or reasoning enabled. Architecture frozen.

## 2. Logging
- [ ] **Reused signals confirmed emitting:** investigation/phase duration
      (`ModuleResult.elapsed_ms` + phase receipts), evidence access (`_evidence_lifecycle`),
      replay usage (replay artifact + `corpus_version`).
- [ ] **Pilot event log wired:** `sentinel_core.oip.pilot_telemetry` records
      `operator_interaction`, `recommendation_usage`, `operator_feedback` to an append-only
      JSONL path owned by the pilot facilitator (caller-supplied timestamps; no wall-clock).
- [ ] Log path is on durable storage and included in backup; events are canonical JSON
      (replayable, diff-able).

## 3. Rollback
- [ ] Rollback = **stop consulting the surfaces** — SentinelAI holds no authority, so there is
      nothing to revert in incident response. Human workflow is unaffected.
- [ ] Code rollback (if a defect is found and a fix is rejected): revert the specific commit;
      OIP services are produce-only and imported by no runtime path, so removal cannot break
      the investigation engine.
- [ ] Pilot event log is additive; deleting it has no runtime effect.

## 4. Monitoring
- [ ] Determinism spot-check scheduled: re-run `eval/ovp/measure_phase1_baseline.py` on the
      pinned corpus weekly; `all_deterministic` must stay true.
- [ ] Verifiability watch: `verification_status.verifiable` on the Daily Brief must remain true
      for corpus-stamped incidents; a drop signals a corpus/replay problem.
- [ ] Event-log growth and `summarize()` reviewed at each pilot checkpoint.
- [ ] Regression gate: full suite green before pilot start (see Go/No-Go).

## 5. Permissions
- [ ] Read-only access to completed investigation artifacts and the OIP surfaces for
      participating operators.
- [ ] Write access to the pilot event log restricted to the facilitator tooling.
- [ ] No operator can trigger remediation *through* SentinelAI (there is no such path).
- [ ] Corpus and learning stores are not writable by pilot participants.

## 6. Operational support
- [ ] Named pilot owner + facilitator per participating rotation.
- [ ] Defect intake channel defined; severity triage follows Go/No-Go categories
      (Blocking / Recommended / Safe to defer).
- [ ] Operator Runbook (`docs/pilot/OPERATOR_RUNBOOK.md`) distributed and walked through.
- [ ] Feedback questionnaire (OVP Phase 1 §5) available at end of each incident.
- [ ] Escalation path: a flagged output → facilitator → engineer; only a **verified defect**
      unfreezes architecture during the pilot.

---

### Sign-off
| Gate | Owner | Status |
|---|---|---|
| Configuration | | ☐ |
| Logging | | ☐ |
| Rollback | | ☐ |
| Monitoring | | ☐ |
| Permissions | | ☐ |
| Operational support | | ☐ |

Pilot may start only when all six gates are signed and the Go/No-Go recommendation is **GO**.
