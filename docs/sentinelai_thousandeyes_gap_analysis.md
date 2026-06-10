# SentinelAI + ThousandEyes RCA Gap Analysis

**Date:** 2026-06-10  
**Purpose:** Map ThousandEyes evidence against SentinelAI's current RCA blind spots  

---

## Current SentinelAI Evidence Sources

| Source | What It Sees |
|--------|-------------|
| Dynatrace | APM traces, service dependencies, CPU/memory, code-level latency |
| Sysdig | Golden signals, Kubernetes events, container metrics |
| Splunk | App logs, error logs, access logs, change records |
| ServiceNow | CMDB, incident history, change tickets |
| GitHub | Deployments, PRs, code diffs |
| Confluence | Runbooks, architecture diagrams |
| Intelligence layer | Pattern memory, dependency graph, resolution memory |

**What none of these see:** the network between users and your infrastructure.

---

## Gap Analysis Table

| # | RCA Scenario | Existing Evidence | Blind Spot | ThousandEyes Evidence | RCA Improvement | Confidence Gain |
|---|-------------|------------------|-----------|----------------------|----------------|----------------|
| 1 | Internal systems healthy, users impacted | Dynatrace: app latency normal. Sysdig: pods healthy. Splunk: no errors. | No view of path between users and edge | `te_list_alerts`: active alert on HTTP test from NY/London. `te_get_test_results`: availability 0% from EU agents | RCA shifts from "unknown" to "network path failure between EU users and CDN edge" | +40 points; avoids false "no issue found" conclusion |
| 2 | App logs clean, users report timeouts | Splunk: no timeout errors in app logs. Dynatrace: normal p95 latency. | App is never receiving the connections; failures occur before TCP connects | `te_get_test_results`: `connectTime` = 0 (never connected). `te_get_path_vis`: packet loss at hop 4 (ISP boundary) | Root cause: TCP handshake failing at ISP boundary, never reaches app | +35 points; explains why app logs are clean |
| 3 | Dynatrace shows high latency, cause unclear | Dynatrace: p95 response time 3x normal. No spike in CPU, memory, or DB calls. | Cannot distinguish network-induced vs. processing latency | `te_get_test_results`: `dnsTime` normal, `connectTime` normal, `waitTime` 3x elevated | Latency is in app processing (waitTime = TTFB after connection), not in network | +25 points; confirms app is the layer to investigate, not network |
| 4 | Dynatrace shows high latency — opposite case | Dynatrace: p95 response time 3x normal. | Cannot distinguish causes | `te_get_test_results`: `connectTime` 3x elevated, `waitTime` normal | Latency is in TCP connection establishment (network), not in app processing | +30 points; deflects from app team to network team |
| 5 | Kubernetes healthy, external users fail | Sysdig: all pods running. Dynatrace: internal service mesh latency normal. | No external vantage point; internal health checks do not traverse internet | `te_list_alerts`: HTTP test alert firing from 3 cloud regions. `te_get_test_results`: availability 0% from external agents | External path or ingress load balancer is failing even though internal pods are healthy | +40 points; narrows to ingress/LB layer |
| 6 | DNS errors in app logs | Splunk: `NXDOMAIN` errors for `payments.example.com`. | Source of DNS failure unknown — internal resolver? Authoritative? CDN DNS? | `te_get_test_results` (DNS test): resolution failing from all agents. DNS server returning `SERVFAIL`. | Authoritative DNS for `payments.example.com` is failing; root cause in DNS infrastructure | +40 points; identifies exact DNS tier |
| 7 | ISP / regional outage | User reports: "slow in Germany." App metrics: normal. Dynatrace: no issue. | Zero ISP or regional visibility | `te_get_test_results`: EU cloud agents show 80% packet loss. US agents show 0%. `te_get_path_vis`: degraded hops at Deutsche Telekom ASN | Regional ISP (DT) degradation affecting EU users; US users unaffected | +35 points; escalation target identified as DT |
| 8 | SaaS dependency degradation | Splunk: timeout errors calling `api.salesforce.com`. Dynatrace: high outbound latency to SaaS. | Cannot determine if SaaS is healthy globally or just for us | `te_get_test_results`: HTTP test to `api.salesforce.com` shows 0% availability from all cloud agents globally | Salesforce global outage confirmed; not our infrastructure | +40 points; immediate escalation to vendor + status page |
| 9 | VPN / endpoint local issue | User reports: "app is slow." App metrics: normal. | No endpoint-level visibility | Enterprise endpoint agents at user's office show: gateway RTT 300ms (normal: 5ms), VPN tunnel packet loss 15% | User's office network or VPN degraded; not the app | +35 points; deflects false P1 to IT/network team |
| 10 | CDN cache miss storm | Splunk: spike in origin server requests. Dynatrace: origin CPU spike. | Cannot determine if CDN is functioning correctly or routing around a bad pop | `te_get_test_results` (page-load): CDN pop in Frankfurt returning 503; requests falling back to origin | Frankfurt CDN pop down; origin overloaded by fallback traffic | +30 points; escalation to CDN vendor |
| 11 | BGP hijack / route leak | Sudden latency spike. No app or infrastructure changes. | No visibility into internet routing changes | BGP test: route to `203.x.x.0/24` changed from 3-hop to 12-hop path via new origin AS; RTT tripled | BGP route change — traffic rerouted through suboptimal path | +35 points; carrier-level escalation with AS evidence |
| 12 | Intermittent packet loss during peak hours | User reports: intermittent timeouts every few minutes. Dynatrace: occasional spikes. | Intermittent — hard to catch in app logs | `te_get_test_results`: packet loss 8–12% recurring every 5 minutes from same ISP. Correlates with app spike window | Congested ISP link during peak hours; queue-based packet dropping | +30 points; carrier capacity issue identified |
| 13 | Multi-region deployment, one region failing | Sysdig: pods healthy in both regions. Dynatrace: no app errors. | Cannot see external connectivity per region | `te_get_test_results`: US-West cloud agents: 100%. US-East cloud agents: 0%. Path vis: US-East paths drop at regional peering | US-East regional peering/interconnect degraded | +35 points; blast radius narrowed to one region |
| 14 | Third-party script blocking page load | User reports: "page is slow/broken." App API metrics: normal. | App API is fine; page load is blocked by third-party dependency | `te_get_test_results` (page-load): waterfall shows `cdn.analytics-vendor.com` timing out, blocking render | Third-party analytics script timeout blocking page load | +25 points; escalation to third-party vendor or add async loading |
| 15 | SSL certificate / TLS issue | Splunk: TLS handshake errors. | Cannot distinguish expired cert vs. protocol mismatch vs. MITM | `te_get_test_results`: `sslTime` spiking. `errorType: SSL_HANDSHAKE_FAILURE` from multiple agents | TLS/SSL layer failure; cert or protocol issue | +30 points; narrows to TLS layer specifically |

---

## Blind Spots Without ThousandEyes: Summary

| Blind Spot Category | Impact | Frequency |
|--------------------|--------|-----------|
| Internet path failures | Miss network-induced timeouts entirely | High |
| ISP/carrier degradation | False "no cause found" RCA | Medium |
| Regional user impact | Cannot scope blast radius correctly | Medium |
| DNS failures (authoritative) | DNS root causes appear as "connection refused" | Medium |
| SaaS outages | Cannot distinguish our failure from vendor failure | High |
| CDN pop failures | CDN layer invisible; appears as origin degradation | Medium |
| VPN/endpoint issues | False P1 escalation to app team | Medium |
| BGP route changes | Cannot explain sudden latency spikes without changes | Low |

---

## High-Value Investigation Types for ThousandEyes Enrichment

Based on the gap analysis, ThousandEyes evidence should be **automatically retrieved** for these incident types:

| Incident Type | ThousandEyes Call Priority |
|--------------|--------------------------|
| `external_timeout` | P0 |
| `latency_spike` (user-reported) | P0 |
| `regional_outage` | P0 |
| `api_unavailable` (external dependency) | P0 |
| `dns_failure` | P0 |
| `intermittent_connectivity` | P1 |
| `high_latency` (cause unknown) | P1 |
| `cdn_degradation` | P1 |
| `saas_dependency_failure` | P1 |
| `vpn_performance` | P2 |
| `page_load_slow` | P2 |
| `ssl_error` | P2 |
