# IE-2 — DNS / Route53 Vertical Slice (Implemented)

The first authorized engine-capability increment: the complete 8-link evidence
pipeline for DNS/Route53, behind `IE_DNS_ENABLED` (default **false**). Validates
the IE-1 architecture — that a full acquisition→reasoning path can be added
safely, deterministically, and without regressing existing behavior.

> **Result:** flag OFF is byte-identical to `main`; flag ON fixes both EFIC DNS
> scenarios (RCA 0→1, confidence into the expected band) and changes **nothing
> else** (only the 2 DNS scenarios differ flag-on vs flag-off). EnterpriseBench,
> EFIC, replay, and the existing analyzers are untouched.

## The 8 links, as built

| Link | Change | File |
|---|---|---|
| 1 classify | *unchanged* — DNS is discovered from evidence, not the summary (DNS-001's summary has no DNS hint) | — |
| 2 acquisition (proof-gated) | Step 3g: probe route53 only when logs show a DNS/named-dependency symptom (mirrors `_find_deployment` gating) | `supervisor/phases/collect.py` |
| 3 params/worker call | `_maybe_fetch_dns_evidence` via `_call_worker` (receipts/budget/circuits) | `supervisor/agent.py` |
| 4 worker | `DnsWorker` → `gateway.invoke("route53.get_record"/"check_resolver", …)`; self-gates to `{}` when flag off | `workers/dns_worker.py` |
| 5 gateway routing | `route53` in `_TOOL_TO_SERVER` + `_stub_route53` (production-shaped empty) | `workers/mcp_client.py` |
| 6 evidence dict | `evidence["dns_evidence"]` (additive key) | `supervisor/phases/collect.py` |
| 7 extraction | `_extract_dns_evidence` | `supervisor/agent.py` |
| 8 reasoning | `_analyze_dns` emits `stale_dns_record` / `dns_resolver_outage`, injected into the hypothesis pool after `_generate_hypotheses` so it competes in scoring, elimination, and winner selection | `supervisor/agent.py` |

**Reasoning fires only on a real DNS problem** (stale record / unhealthy
resolver); a probe that returns clean or empty DNS contributes nothing — so DNS
evidence changes the diagnosis only when appropriate.

## Flag discipline (why flag-off is byte-identical)

Every DNS path is gated by `_ie_dns_enabled()` (read at call time):
`DnsWorker` is not even registered when off; the probe returns `None`; the
analyzer injection is skipped. The EB-2 twin mirrors this — `render.py` serves the
route53 channel and `BenchMCPSource.discover_tools()` advertises `route53` **only**
when the flag is on, so the flag-off report hashes identically to the EB-3
baseline.

## EnterpriseBench (EB-2) evidence

- **Flag OFF full corpus** `content_hash = 3e277b87d9bc31db` — **identical** to the
  pre-IE-2 baseline (byte-identical behavior).
- **Flag ON**: `EFIC-DNS-001` RCA 0→1 (“stale Route53 DNS record (db.catalog)
  points catalog-service to a decommissioned endpoint”), confidence 87 ∈ [70,90],
  EIC 0.29→0.61; `EFIC-DNS-RESOLVER-001` RCA 0→1 (“internal DNS resolver outage
  causing NXDOMAIN / name resolution failures”), confidence 86 ∈ [72,93]. Both now
  query `route53`.
- **Flag ON, everything else**: only the 2 DNS scenarios differ vs flag-off
  (verified by per-scenario diagnosis diff). Mean EIC 0.278→0.299.
- **Determinism**: flag-off and flag-on full runs each reproduce byte-identically
  (`3e277b87d9bc31db` / `be1af2b9c6bf7e8d`).

## Production safety

Additive only; existing analyzer bodies untouched; the gateway transport
untouched; in production without a Route53 MCP connected, `discover_tools()` omits
`route53` ⇒ `DnsWorker` is skipped even with the flag on (a second safety layer).
Tests: `tests/enterprisebench/test_ie2_dns.py` (12) — flag-off inertness, route53
stub empty, render flag-gating, DNS reasoning (stale/outage/silent-on-clean),
probe gate, worker non-registration, and end-to-end flag-on-fixes / flag-off-
unchanged.

## Known limitations & next

- Outcome remains `FAIL` for the DNS scenarios (EIC composite 0.46–0.61 < 0.70
  pass threshold) even though RCA and confidence are now correct — the composite is
  dragged by dimensions like evidence-efficiency/decisive-latency that reflect the
  broader engine, not DNS. IE-2's success criterion is *quality improvement + no
  regression*, not crossing the pass threshold.
- Classification is deliberately unchanged; DNS is evidence-discovered. A future
  refinement could add flag-gated DNS keywords for summaries that do hint at DNS.

**Recommendation for IE-3:** apply the identical, now-validated pattern to the next
highest decisive-value domain — **AWS/CloudWatch** (5 EFIC scenarios, S3-throttling
and AZ-impairment decisive) — behind `IE_AWS_ENABLED`, one domain per reviewed,
flag-gated, EB-2-validated increment. Do not broaden to multiple domains at once.
