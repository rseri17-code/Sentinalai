# ThousandEyes Deterministic Correlation Rules

**Date:** 2026-06-10  
**Design principle:** Every rule must produce a deterministic output from observable inputs. No LLM inference in the rule logic itself — LLM only for narrative generation.

---

## Rule Format

Each rule:
- Has a unique name and ID
- Specifies exact input signals required
- Defines a boolean or scored logic expression
- Produces a confidence delta (+/-)
- Assigns a recommended owner
- Outputs a structured RCA string
- References a test fixture for validation

---

## Rule 1: Network-Induced Latency

**Rule ID:** `TE-CORR-001`  
**Rule Name:** `network_induced_latency`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| App latency spike | Dynatrace | `response_time_p95` | > 2× baseline |
| Network packet loss | ThousandEyes | `te_get_test_results.packetLoss` | > 2% from ≥2 agents |
| App connect time | ThousandEyes | `te_get_test_results.connectTime` | > 2× baseline |
| App wait time | ThousandEyes | `te_get_test_results.waitTime` | ≤ 1.5× baseline |

**Logic:**
```python
def rule_network_induced_latency(dynatrace_latency, te_results):
    app_latency_spike = dynatrace_latency.p95 > dynatrace_latency.baseline * 2
    packet_loss = [r for r in te_results if r.packetLoss > 2.0]
    connect_elevated = [r for r in te_results if r.connectTime > te_results.baseline_connect * 2]
    wait_normal = [r for r in te_results if r.waitTime <= te_results.baseline_wait * 1.5]
    
    if (app_latency_spike and 
        len(packet_loss) >= 2 and 
        len(connect_elevated) >= 2 and 
        len(wait_normal) >= 2):
        return True, "NETWORK_INDUCED_LATENCY"
    return False, None
```

**Confidence impact:** +30 points  
**Recommended owner:** Network / ISP  
**RCA output wording:**
```
Network-induced latency detected. ThousandEyes shows packet loss of {X}% and 
elevated TCP connect time ({Y}ms vs baseline {Z}ms) from {N} agents, while 
app processing time (waitTime) remains normal. The latency is transport-layer, 
not application-layer. Escalate to network team / carrier.
```
**Test fixture needed:** `test_network_induced_latency.json` — TE results with packetLoss=8%, connectTime=450ms, waitTime=normal; Dynatrace p95=3x baseline.

---

## Rule 2: External Network Degradation (Splunk + TE)

**Rule ID:** `TE-CORR-002`  
**Rule Name:** `external_network_degradation`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| Connection timeout errors | Splunk | log pattern: `ETIMEDOUT\|connection timed out\|connect timeout` | ≥10 in window |
| Route instability | ThousandEyes | `te_get_path_vis` hop changes | ≥1 new or changed hop |
| Path RTT increase | ThousandEyes | `te_get_path_vis` max hop RTT | > 3× previous round |

**Logic:**
```python
def rule_external_network_degradation(splunk_errors, te_path_vis):
    timeout_errors = count_pattern(splunk_errors, r'ETIMEDOUT|connection timed out|connect timeout')
    changed_hops = [h for h in te_path_vis.hops if h.is_new or h.rtt_change_ratio > 3]
    
    if timeout_errors >= 10 and len(changed_hops) >= 1:
        return True, "EXTERNAL_NETWORK_DEGRADATION"
    return False, None
```

**Confidence impact:** +35 points  
**Recommended owner:** Network / ISP / Carrier  
**RCA output wording:**
```
External network degradation detected. Splunk shows {N} connection timeout errors 
correlated with ThousandEyes path change: hop {hop_num} ({hop_rdns}) added/changed 
with RTT increase from {before}ms to {after}ms. This is likely ISP or transit 
provider degradation, not an application-layer issue.
```
**Test fixture needed:** `test_external_network_degradation.json` — Splunk with 45 ETIMEDOUT events; TE path-vis with new hop at position 6, RTT=450ms vs previous 12ms.

---

## Rule 3: Infrastructure Healthy, External Path Degraded

**Rule ID:** `TE-CORR-003`  
**Rule Name:** `infra_healthy_path_degraded`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| K8s pods healthy | Sysdig | pod phase, restart count | All pods Running, restarts=0 |
| App error rate normal | Dynatrace | internal error rate | < baseline + 1σ |
| External HTTP test failing | ThousandEyes | `te_get_test_results.availability` | < 50% from ≥2 external agents |

**Logic:**
```python
def rule_infra_healthy_path_degraded(sysdig, dynatrace, te_results):
    pods_healthy = all(p.phase == "Running" and p.restarts == 0 for p in sysdig.pods)
    app_errors_normal = dynatrace.internal_error_rate < dynatrace.baseline + dynatrace.stddev
    external_failing = [r for r in te_results if r.availability < 50 and r.agent_type == "cloud"]
    
    if pods_healthy and app_errors_normal and len(external_failing) >= 2:
        return True, "INFRA_HEALTHY_PATH_DEGRADED"
    return False, None
```

**Confidence impact:** +40 points  
**Recommended owner:** Network / CDN / Ingress  
**RCA output wording:**
```
App infrastructure is healthy (all pods running, internal error rate normal) but 
ThousandEyes external agents report {X}% availability from {N} locations. The 
failure is in the external path — CDN, ingress load balancer, or internet 
routing — not in the application layer.
```
**Test fixture needed:** `test_infra_healthy_path_degraded.json` — Sysdig: 12 pods running, 0 restarts; Dynatrace: error_rate=0.1%; TE: 3 cloud agents showing 0% availability.

---

## Rule 4: DNS Root Cause

**Rule ID:** `TE-CORR-004`  
**Rule Name:** `dns_root_cause`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| DNS errors in app logs | Splunk | `NXDOMAIN\|SERVFAIL\|dns resolution\|getaddrinfo` | ≥5 in window |
| DNS test failure | ThousandEyes | `te_get_test_results` (DNS type) availability | < 80% |
| DNS resolution time | ThousandEyes | `te_get_test_results.dnsTime` | > 3× baseline OR errorType contains "DNS" |

**Logic:**
```python
def rule_dns_root_cause(splunk_logs, te_dns_results):
    dns_errors_in_logs = count_pattern(splunk_logs, r'NXDOMAIN|SERVFAIL|dns resolution|getaddrinfo')
    dns_tests_failing = [r for r in te_dns_results if r.availability < 80]
    dns_slow = [r for r in te_dns_results if r.dnsTime > te_dns_results.baseline_dns * 3]
    
    if dns_errors_in_logs >= 5 and (len(dns_tests_failing) >= 1 or len(dns_slow) >= 2):
        return True, "DNS_ROOT_CAUSE"
    return False, None
```

**Confidence impact:** +40 points  
**Recommended owner:** DNS provider / Platform DNS team  
**RCA output wording:**
```
DNS failure detected as root cause. Splunk shows {N} DNS resolution errors. 
ThousandEyes DNS tests confirm: {X}% availability from {M} agents (baseline: ~100%). 
DNS resolution time elevated to {T}ms (baseline {B}ms). DNS infrastructure for 
{target} is failing. Engage DNS provider or check internal resolver health.
```
**Test fixture needed:** `test_dns_root_cause.json` — Splunk: 23 SERVFAIL errors; TE DNS test: 2 agents showing 0% availability, dnsTime=2000ms.

---

## Rule 5: Regional ISP / Network Issue

**Rule ID:** `TE-CORR-005`  
**Rule Name:** `regional_isp_issue`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| Geographically clustered failures | ThousandEyes | `te_get_test_results.agentName` grouped by region | ≥2 agents in same region failing |
| Other regions healthy | ThousandEyes | `te_get_test_results` from other regions | availability ≥90% |
| Common ISP in failing path | ThousandEyes | `te_get_path_vis` hop ASN | Same ASN in all failing paths |

**Logic:**
```python
def rule_regional_isp_issue(te_results, te_path_vis):
    by_region = group_by_region(te_results)
    failing_regions = [r for r, results in by_region.items() 
                       if mean(results, 'availability') < 50]
    healthy_regions = [r for r, results in by_region.items() 
                       if mean(results, 'availability') >= 90]
    
    if len(failing_regions) >= 1 and len(healthy_regions) >= 1:
        common_asn = find_common_asn_in_failing_paths(te_path_vis, failing_regions)
        if common_asn:
            return True, "REGIONAL_ISP_ISSUE", {
                "failing_regions": failing_regions,
                "healthy_regions": healthy_regions,
                "asn": common_asn
            }
    return False, None, {}
```

**Confidence impact:** +35 points  
**Recommended owner:** Regional ISP (named by ASN)  
**RCA output wording:**
```
Regional network issue detected. ThousandEyes agents in {failing_regions} report 
{X}% availability; agents in {healthy_regions} report normal availability. 
All failing paths traverse AS{asn} ({provider_name}). This is a regional ISP/carrier 
issue, not a global outage. Escalate to {provider_name} with ThousandEyes path data.
```
**Test fixture needed:** `test_regional_isp_issue.json` — TE results: EU agents 0% availability, US/APAC agents 100%; path-vis: EU paths all traverse AS1299 (Telia).

---

## Rule 6: SaaS Provider Outage

**Rule ID:** `TE-CORR-006`  
**Rule Name:** `saas_provider_outage`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| External API timeout | Splunk | `connection refused\|timeout.*{saas_domain}` | ≥5 in window |
| SaaS test failing globally | ThousandEyes | `te_get_test_results` for SaaS target | availability <50% from ≥3 diverse agents |
| Internal systems unaffected | Dynatrace | internal service latency | All ≤ 1.2× baseline |

**Logic:**
```python
def rule_saas_provider_outage(splunk_logs, te_saas_results, dynatrace_metrics, saas_domain):
    external_errors = count_pattern(splunk_logs, rf'timeout.*{re.escape(saas_domain)}')
    saas_failing = [r for r in te_saas_results if r.availability < 50]
    agent_diversity = len(set(r.agent_network for r in saas_failing))  # different ISPs
    internal_healthy = all(m.latency < m.baseline * 1.2 for m in dynatrace_metrics.internal)
    
    if external_errors >= 5 and len(saas_failing) >= 3 and agent_diversity >= 2 and internal_healthy:
        return True, "SAAS_PROVIDER_OUTAGE"
    return False, None
```

**Confidence impact:** +40 points  
**Recommended owner:** SaaS vendor (external)  
**RCA output wording:**
```
SaaS provider outage detected. {saas_domain} is unreachable from {N} ThousandEyes 
agents across {M} different networks ({agent_list}). Our internal services are 
healthy. This is a {saas_vendor} infrastructure issue. Check {saas_vendor} status 
page and escalate to vendor support. Activate SaaS fallback/circuit-breaker if available.
```
**Test fixture needed:** `test_saas_provider_outage.json` — TE HTTP test to `api.salesforce.com`: 5 cloud agents across 4 ISPs all showing 0% availability; Dynatrace: internal metrics all normal.

---

## Rule 7: Endpoint-Only Failure (VPN / Local Network)

**Rule ID:** `TE-CORR-007`  
**Rule Name:** `endpoint_local_network_failure`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| User-reported performance issue | Incident description / Moogsoft | `incident_type=user_reported_slowness` | present |
| Cloud agents healthy | ThousandEyes | `te_get_test_results` cloud agents | availability ≥95% |
| Enterprise endpoint agents degraded | ThousandEyes | `te_get_test_results` enterprise/endpoint agents | availability <50% OR latency >3× baseline |
| Location correlation | ThousandEyes | `te_list_agents` agent location | degraded agents co-located with reporting users |

**Logic:**
```python
def rule_endpoint_local_failure(incident, te_cloud_results, te_enterprise_results):
    cloud_healthy = mean(te_cloud_results, 'availability') >= 95
    enterprise_degraded = [r for r in te_enterprise_results if r.availability < 50 or r.latency > r.baseline * 3]
    
    if (incident.user_reported and 
        cloud_healthy and 
        len(enterprise_degraded) >= 1):
        return True, "ENDPOINT_LOCAL_NETWORK_FAILURE", {
            "affected_locations": [r.agent_location for r in enterprise_degraded]
        }
    return False, None, {}
```

**Confidence impact:** +35 points  
**Recommended owner:** IT / End-user network team  
**RCA output wording:**
```
Endpoint/local network failure detected. Cloud-based ThousandEyes agents report 
{X}% availability (normal). Enterprise agents at {affected_locations} report 
{Y}% availability and {Z}ms latency (baseline: {B}ms). The issue is local to 
{affected_locations} — likely VPN, office Wi-Fi, or ISP at that location. 
Escalate to IT/network team, not the app team.
```
**Test fixture needed:** `test_endpoint_local_failure.json` — Cloud agents: 100% availability; 2 NYC enterprise agents: 0% availability with error "gateway timeout"; incident type: user_reported_slowness.

---

## Rule 8: SSL / TLS Degradation

**Rule ID:** `TE-CORR-008`  
**Rule Name:** `ssl_tls_degradation`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| TLS errors in app logs | Splunk | `SSL\|TLS\|certificate\|handshake` errors | ≥3 in window |
| SSL time elevated | ThousandEyes | `te_get_test_results.sslTime` | > 3× baseline |
| Error type | ThousandEyes | `te_get_test_results.errorType` | `SSL_HANDSHAKE_FAILURE` OR `CERTIFICATE_ERROR` |

**Confidence impact:** +30 points  
**Recommended owner:** Platform / Certificate management  
**RCA output wording:**
```
TLS/SSL degradation detected. ThousandEyes reports SSL handshake time of {X}ms 
(baseline {B}ms) with error type "{error_type}" from {N} agents. App logs confirm 
{M} TLS errors. Check certificate expiry, TLS configuration, and cipher suite compatibility.
```

---

## Rule 9: CDN Edge Failure

**Rule ID:** `TE-CORR-009`  
**Rule Name:** `cdn_edge_failure`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| Origin load spike | Dynatrace | origin request rate | > 3× baseline (CDN cache miss storm) |
| CDN HTTP response errors | ThousandEyes | `te_get_test_results.responseCode` | 5xx from agents routing through CDN |
| Geographic isolation | ThousandEyes | agents grouped by CDN pop | ≥1 region all-failing, others healthy |

**Confidence impact:** +30 points  
**Recommended owner:** CDN vendor  

---

## Rule 10: BGP Route Change Correlation

**Rule ID:** `TE-CORR-010`  
**Rule Name:** `bgp_route_change_correlation`

**Inputs required:**
| Input | Source | Field | Condition |
|-------|--------|-------|-----------|
| Sudden latency spike | Dynatrace / ThousandEyes | p95 or test RTT | Jump ≥3× in single round |
| No infrastructure changes | ServiceNow / GitHub | change window | No changes in preceding 30 minutes |
| BGP prefix change | ThousandEyes | BGP test data | New origin ASN or increased hop count |

**Confidence impact:** +35 points  
**Recommended owner:** Carrier / Peering team  
**RCA output wording:**
```
BGP route change correlated with incident. ThousandEyes BGP monitoring shows prefix 
{prefix} origin changed from AS{old} to AS{new} at {timestamp}, coinciding with 
latency increase from {before}ms to {after}ms. No internal changes in the preceding 
30 minutes. This is a carrier/BGP routing issue. Open NOC ticket with upstream carrier.
```

---

## Rule Application Order (Recommended)

```
1. TE-CORR-006 (SaaS outage)          — highest confidence; eliminates our blame
2. TE-CORR-004 (DNS root cause)        — DNS is binary; high confidence when firing
3. TE-CORR-007 (Endpoint local)        — eliminates false P1s quickly
4. TE-CORR-003 (Infra healthy, path bad) — rules out app stack
5. TE-CORR-005 (Regional ISP)          — narrows blast radius
6. TE-CORR-001 (Network-induced latency) — app vs. network split
7. TE-CORR-002 (External degradation)  — confirms external path
8. TE-CORR-010 (BGP change)            — carrier-level attribution
9. TE-CORR-009 (CDN failure)           — CDN attribution
10. TE-CORR-008 (SSL/TLS)              — TLS layer isolation
```

---

## Composite Rule: Network Blind Spot Gate

**Rule ID:** `TE-GATE-001`  
**Rule Name:** `network_blind_spot_gate`

If `ENABLE_THOUSANDEYES_RCA=false` or no ThousandEyes tests are configured for the affected service, inject this warning into the RCA output:

```
EVIDENCE GAP: No network observability data available for this investigation.
ThousandEyes integration is not enabled (ENABLE_THOUSANDEYES_RCA=false) or 
no tests are configured for service "{service}". If users are reporting 
timeouts or slowness and internal metrics appear healthy, consider enabling 
ThousandEyes monitoring. Current RCA confidence may be understated.
```

This ensures operators are aware of the blind spot rather than receiving false high-confidence RCA.
