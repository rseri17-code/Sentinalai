# ThousandEyes RCA Value Map

**Perspective:** Chief Network Engineer + Observability Architect  
**Date:** 2026-06-10  
**Question answered:** What can ThousandEyes reveal that app/platform observability tools cannot?

---

## Why Network Intelligence Is a Distinct Evidence Layer

App-layer tools (Dynatrace, Splunk, Sysdig, ServiceNow) are **inside-out observers**: they see what the app sees — its own latency, its own error rates, its own resource consumption. They are blind to:

- What the network between the app and the user looks like
- Whether a packet arrived mangled, delayed, or not at all
- Whether an ISP is routing traffic suboptimally
- Whether DNS resolves differently for users in Frankfurt vs. New York
- Whether a CDN pop is unhealthy
- Whether the SaaS API the app depends on is slow for everyone or just for this region

ThousandEyes is an **outside-in** and **cross-path** observer. Its agents sit in ISPs, cloud regions, corporate offices, and end-user devices — they observe the network exactly as your users do. This is the visibility gap that produces the most misleading RCA: "our app is healthy, but users are complaining."

---

## Signal Map: What ThousandEyes Reveals

---

### 1. Internet Path Degradation

| Data Available | RTT per hop, packet loss per hop, jitter per segment, path changes |
|---|---|
| **RCA question answered** | Is there a congested or failed transit segment between our edge and the user? |
| **Failure pattern detected** | Specific hop (e.g., hop 7 at ISP peer) shows >30% packet loss while all prior hops are clean |
| **Likely owner** | ISP / Transit provider |
| **Confidence contribution** | +20–35 points — removes app stack from blame when path is clearly degraded |

---

### 2. Packet Loss

| Data Available | Packet loss % per agent per round (BGP, ICMP, TCP-based) |
|---|---|
| **RCA question answered** | Is data being dropped in transit before reaching the server? |
| **Failure pattern detected** | Network packet loss without CPU/memory pressure on app side |
| **Likely owner** | ISP / Network infrastructure |
| **Confidence contribution** | +25 points when loss >5% co-occurs with user-reported latency |

---

### 3. Latency and Jitter

| Data Available | Round-trip time per round (ms), jitter (variation in RTT), one-way delay estimates |
|---|---|
| **RCA question answered** | Is observed app latency caused by network transport or application processing? |
| **Failure pattern detected** | `connectTime` normal but `waitTime` elevated = app processing slow. `connectTime` elevated = network slow |
| **Likely owner** | App (if waitTime) / Network (if connectTime/RTT) |
| **Confidence contribution** | +15–30 points — splits "latency" into network-induced vs. app-induced |

---

### 4. BGP Route Changes

| Data Available | BGP prefix withdrawals, origin ASN changes, route instability events, RPKI validation failures |
|---|---|
| **RCA question answered** | Has an internet routing change rerouted traffic through a suboptimal or congested path? |
| **Failure pattern detected** | Route change timestamp correlates with latency spike start; new path is 3x longer |
| **Likely owner** | ISP / Regional carrier / Internet exchange |
| **Confidence contribution** | +35 points — BGP events are highly specific; when correlated with incident start time, near-certain attribution |

---

### 5. ASN / Provider Issues

| Data Available | Autonomous System Number per hop, provider name via reverse DNS and routing databases |
|---|---|
| **RCA question answered** | Which specific ISP or cloud provider's network is the failure point? |
| **Failure pattern detected** | All degraded paths traverse AS64512 (carrier X); healthy paths do not |
| **Likely owner** | Named ISP / Transit provider |
| **Confidence contribution** | +25 points — enables escalation to carrier with specific AS evidence |

---

### 6. ISP / Carrier Degradation

| Data Available | Multiple cloud agents in same geography all failing toward same destination via same ISP |
|---|---|
| **RCA question answered** | Is this a provider-specific issue affecting all customers of that ISP? |
| **Failure pattern detected** | NYC Comcast agents fail; NYC Verizon agents succeed → Comcast-specific degradation |
| **Likely owner** | ISP (escalation target) |
| **Confidence contribution** | +30 points — rules out internal cause when degradation is ISP-correlated |

---

### 7. DNS Failures

| Data Available | DNS resolution time, NXDOMAIN responses, wrong IP returned, DNS server availability |
|---|---|
| **RCA question answered** | Is a DNS failure preventing the app from reaching dependencies? Or preventing users from resolving our domain? |
| **Failure pattern detected** | DNS resolution time spikes from 5ms to 2000ms; intermittent SERVFAIL responses |
| **Likely owner** | DNS provider / Internal DNS infrastructure |
| **Confidence contribution** | +40 points — DNS is binary (works or it doesn't); TE DNS tests are highly specific |

---

### 8. DNS Latency

| Data Available | DNS resolution time per resolver per agent per round (milliseconds) |
|---|---|
| **RCA question answered** | Is slow DNS adding hidden latency to every connection? |
| **Failure pattern detected** | DNS time jumps from 8ms to 400ms, explaining 40% of observed response time increase |
| **Likely owner** | DNS provider / CDN DNS / Internal resolver |
| **Confidence contribution** | +20 points when DNS time accounts for significant fraction of total response time |

---

### 9. CDN / Edge Issues

| Data Available | HTTP response from CDN edge nodes, cache hit/miss indicators, origin pull latency |
|---|---|
| **RCA question answered** | Is the CDN's edge layer healthy? Is it correctly serving content or falling back to origin? |
| **Failure pattern detected** | CDN pop in Frankfurt returning 503; other pops healthy → regional CDN pop failure |
| **Likely owner** | CDN provider (Cloudflare, Fastly, Akamai, CloudFront) |
| **Confidence contribution** | +25 points — CDN failures look like app failures to internal monitoring |

---

### 10. SaaS / Provider Outages

| Data Available | HTTP availability and response time to external SaaS APIs (Salesforce, Workday, AWS, Azure, GCP) |
|---|---|
| **RCA question answered** | Is a SaaS dependency experiencing an outage that explains our app failures? |
| **Failure pattern detected** | ThousandEyes shows Salesforce API availability drops to 0% at same time as our CRM sync failures |
| **Likely owner** | SaaS provider (external, not our team) |
| **Confidence contribution** | +40 points — eliminates our-side blame entirely when SaaS is confirmed down |

---

### 11. Endpoint / User Network Issues

| Data Available | Enterprise endpoint agents report local network conditions: Wi-Fi signal, gateway RTT, VPN tunnel health |
|---|---|
| **RCA question answered** | Is the issue affecting only specific users because of their local network, not our infrastructure? |
| **Failure pattern detected** | Only employees in Chicago report slow access; ThousandEyes endpoint agents in Chicago show gateway packet loss |
| **Likely owner** | End-user / Office network / IT |
| **Confidence contribution** | +35 points — deflects false-positive P1 escalations for local/VPN issues |

---

### 12. VPN / Wi-Fi / Local Gateway

| Data Available | VPN connection status, tunnel RTT, gateway availability, wireless signal strength |
|---|---|
| **RCA question answered** | Is VPN or Wi-Fi contributing to user-reported degradation? |
| **Failure pattern detected** | VPN agents show elevated RTT (200ms vs normal 20ms); non-VPN agents show normal performance |
| **Likely owner** | Network/IT team |
| **Confidence contribution** | +30 points — definitively separates VPN-induced from app-induced |

---

### 13. Page Load / API Transaction Failures

| Data Available | Full page load waterfall (DNS, TCP, TLS, TTFB, content download per resource), multi-step API transactions |
|---|---|
| **RCA question answered** | Which specific resource or API step is failing or slow? |
| **Failure pattern detected** | Page load fails on third-party analytics script (ads.tracker.com) timing out, blocking render |
| **Likely owner** | Third-party provider |
| **Confidence contribution** | +20 points — isolates third-party vs. first-party failures |

---

### 14. Regional User Impact

| Data Available | Per-agent (per-region) availability and latency; geographic map of which agents are impacted |
|---|---|
| **RCA question answered** | Is this a global outage or affecting one region? |
| **Failure pattern detected** | US-East agents: 100% availability. EU agents: 0% availability → EU-specific routing issue |
| **Likely owner** | Regional network / CDN / Peering |
| **Confidence contribution** | +20 points — changes blast radius assessment from global to regional |

---

## Signal Confidence Contribution Summary

| Signal | Confidence Contribution | Primary Use in RCA |
|--------|------------------------|-------------------|
| BGP route change correlates with incident | +35 | Carrier attribution |
| DNS failure | +40 | DNS root cause |
| SaaS provider down | +40 | External dependency attribution |
| Path hop failure (specific ISP) | +25–35 | ISP/carrier blame |
| Endpoint/VPN only impacted | +35 | Local/VPN cause deflection |
| Latency splits by timing component | +15–30 | App vs. network split |
| CDN edge failure | +25 | CDN vendor attribution |
| Packet loss (network only) | +25 | Network infrastructure |
| Regional isolation | +20 | Blast radius narrowing |
| DNS latency accounts for response time | +20 | Hidden DNS cost |
| Page load waterfall step failure | +20 | Third-party isolation |
| ISP-specific agent failure pattern | +30 | ISP-specific attribution |

---

## What ThousandEyes Cannot Do

| Limitation | Impact |
|-----------|--------|
| Cannot see inside app servers | No code-level RCA |
| Cannot see database query times | Requires APM (Dynatrace) |
| Cannot see log errors | Requires Splunk |
| Cannot see Kubernetes pod state | Requires Sysdig |
| Cannot see change history | Requires ServiceNow/Git |
| Cannot see service-to-service calls | Requires distributed tracing |
| Cannot see memory/CPU inside app | Requires APM metrics |
| Enterprise agents offline = no data | Gaps in coverage |
| No tests configured = no data | Requires pre-configured tests |
