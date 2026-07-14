# Operator Labeling Framework
**Deliverable 3 · DESIGN ONLY — no ServiceNow integration**

Ground truth is the binding constraint on certification (3 labeled incidents today vs the
500 / 20-per-class gate). This framework defines *how* operators supply labels; it does not
build an integration.

## Canonical label model
| Field | Type | Meaning |
|---|---|---|
| `verdict` | enum | `ROOT_CAUSE_CORRECT` · `ROOT_CAUSE_PARTIAL` · `ROOT_CAUSE_INCORRECT` · `UNKNOWN` |
| `validated_root_cause` | text | the operator-confirmed cause |
| `actual_remediation` | text | what actually fixed it |
| `resolution_time_ms` | int | measured resolution time |
| `operator_confidence` | 0–100 | operator's confidence in the label |
| `operator_comments` | text | free-form |
| `false_positive` | bool | SentinelAI flagged a cause that was not real |
| `false_negative` | bool | the real cause was never surfaced |
| `supporting_evidence` | list | evidence the operator found decisive |
| `missing_evidence` | list | evidence SentinelAI should have collected |

The engine's `_normalise_label()` consumes exactly this envelope and defaults to
`UNKNOWN`/`labeled=false` when absent — unlabeled investigations never inflate accuracy.

## Verdict semantics
- **CORRECT** → counts toward RCA accuracy numerator; calibration uses correct=1.0.
- **PARTIAL** → excluded from the strict accuracy numerator (documented), tracked
  separately; prevents "partially right" inflating the headline metric.
- **INCORRECT** → accuracy denominator only; feeds the failure taxonomy.
- **UNKNOWN** → `NOT_MEASURED`; never scored.

## Labeling protocol (design)
1. On incident resolution, the operator opens the shadow observation for that incident.
2. They record the verdict + validated cause + remediation + resolution time.
3. Labels are append-only and versioned; a corrected label supersedes but never deletes.
4. **Leakage rule:** incidents used to *develop* the tranches must be flagged and excluded
   from the held-out evaluation partition (train / eval / held-out).

## Explicitly out of scope
No ServiceNow/ITSM write-back, no automation, no runtime coupling. When an integration is
later built, it must produce exactly this envelope so the engine is unchanged.
