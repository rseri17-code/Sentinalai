# IE-3 — Identity / IAM Vertical Slice (Implemented)

The second engine-capability increment, applying the IE-2-validated pattern to a
different reasoning class: authentication and authorization. Behind
`IE_IDENTITY_ENABLED` (default **false**).

> **Result:** flag OFF is byte-identical to `main`; flag ON fixes both EFIC
> identity scenarios (RCA 0→1, confidence into the expected band) and
> **distinguishes authN from authZ**, changing nothing else. EnterpriseBench,
> EFIC, replay, and the existing analyzers are untouched.

## The two reasoning classes

| Scenario | Class | Decisive evidence | Emitted hypothesis |
|---|---|---|---|
| `EFIC-IDENTITY-001` | **Authentication** | expired token signing key (`kid=k-2024`) + JWT signature failures | `signing_key_expiry` |
| `EFIC-IAM-001` | **Authorization** | IAM policy change removing `s3:GetObject` + AccessDenied | `iam_permission_revoked` |

`_analyze_identity` routes on the evidence shape — a signing-key status yields an
authentication hypothesis; a deny/revoke policy change yields an authorization
hypothesis — so the two failure classes are distinguished, as the mission requires.

## The 8 links (same architecture as IE-2)

| Link | Change | File |
|---|---|---|
| 1 classify | *unchanged* — identity is evidence-discovered (summaries carry no auth keyword) | — |
| 2 acquisition (proof-gated) | Step 3h: probe identity only when logs show an authN/authZ symptom (JWT / signature / access-denied / not-authorized / 401 / 403 / token) | `supervisor/phases/collect.py` |
| 3 params/worker call | `_maybe_fetch_identity_evidence` via `_call_worker` | `supervisor/agent.py` |
| 4 worker | `IdentityWorker` → `identity.check_token_signing` (authN) / `identity.get_policy_changes` (authZ) | `workers/identity_worker.py` |
| 5 gateway routing | `identity` in `_TOOL_TO_SERVER` + `_stub_identity` | `workers/mcp_client.py` |
| 6 evidence dict | `evidence["identity_evidence"]` | `supervisor/phases/collect.py` |
| 7 extraction | `_extract_identity_evidence` | `supervisor/agent.py` |
| 8 reasoning | `_analyze_identity` → `signing_key_expiry` / `iam_permission_revoked`, injected into the hypothesis pool | `supervisor/agent.py` |

Reasoning fires only on a real identity problem (expired/invalid key, or a
deny/revoke policy change); clean/empty identity evidence contributes nothing.

## False-infrastructure rejection

Both scenarios carry infrastructure traps (`network`, `s3 outage`, `ldap outage`).
The high-scored, evidence-cited identity hypothesis wins winner-selection over the
generic infrastructure hypotheses, so the false hypotheses are correctly rejected.

## EnterpriseBench (EB-2) evidence

- **Flag OFF full corpus** `content_hash = 3e277b87d9bc31db` — **identical** to the
  pre-IE-2 baseline (byte-identical behavior; IE-2 and IE-3 both off).
- **Flag ON**: `EFIC-IDENTITY-001` RCA 0→1 (“expired token signing key (k-2024) in
  auth-service causing JWT signature validation failures”), confidence 80 ∈ [76,95];
  `EFIC-IAM-001` RCA 0→1 (“IAM policy change revoked reporting-batch permission
  (s3:GetObject denied)”), confidence 80 ∈ [72,92]. Both query `identity`.
- **Flag ON, everything else**: only the 2 identity scenarios differ vs flag-off
  (per-scenario diagnosis diff). Mean EIC 0.278→0.300.
- **Determinism**: flag-off (`3e277b87d9bc31db`) and flag-on (`c9d08820c6472449`)
  full runs each reproduce byte-identically.

## Production safety

Additive only; existing analyzer bodies and the gateway transport untouched; the
`IdentityWorker` is registered only when the flag is on, and gated on tool
availability (`_WORKER_SERVERS["identity_worker"]={"identity"}`) so prod without an
Identity MCP skips it even flag-on. Tests: `tests/enterprisebench/test_ie3_identity.py`
(12) — flag-off inertness, identity stub empty, render flag-gating, authN/authZ
reasoning, silent-on-clean, probe gate, worker non-registration, and end-to-end.

## Known limitations & next

- As with IE-2, the DNS/identity scenarios remain `FAIL` at the **composite**
  threshold (EIC 0.59–0.61 < 0.70) even with RCA and confidence correct — dragged
  by engine-wide dimensions, not identity. Success here is *quality improvement +
  no regression*.
- Classification is again unchanged (evidence-discovery). Certificate-based
  authentication was in scope only "where required for the identity flow"; neither
  EFIC identity scenario needs it, so it is intentionally not built.

**Recommendation for IE-4:** the two remaining reasoning-blocked domains
(ThousandEyes, CMDB) — link-8 only (evidence already fetched) — behind their own
flags, or the next acquisition-blocked domain (AWS/CloudWatch). One reviewed,
flag-gated, EB-2-validated increment at a time.
