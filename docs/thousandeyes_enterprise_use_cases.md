# ThousandEyes Enterprise Use Cases

**Date:** 2026-06-10  
**Count:** 25 realistic SentinelAI + ThousandEyes use cases  
**Prioritized sectors:** Financial services, hybrid cloud, Kubernetes, SaaS dependencies, DNS, internet-facing apps, VPN/endpoint, regional

---

## Financial Services

---

### UC-01: Trading Platform Latency Spike — ISP Induced

**Incident title:** FIX protocol latency spike — London to NYSE co-location  
**Symptom:** Trade execution latency increased from 4ms to 85ms. P1 alert. Desk reporting missed fills.  
**Existing SentinelAI evidence:** Dynatrace shows outbound socket wait time elevated. App logs clean. No deployments.  
**ThousandEyes evidence:** Agent-to-agent test (London → NYC co-lo) shows RTT increase from 6ms to 82ms. Path vis: 3 new hops added at Telia ASN. BGP test: prefix route changed.  
**Final RCA:** Telia transit route change added 3 intercontinental hops, adding 76ms round-trip.  
**Recommended owner:** Network/Peering team — escalate to Telia with BGP evidence  
**Confidence change:** 30 → 82 (+52)  
**Memory stored:** Pattern "BGP route change → latency spike" for London trading path; Telia as recurring risk provider  

---

### UC-02: Payments API Timeout — DNS Failure

**Incident title:** Payments processing failures — NXDOMAIN errors  
**Symptom:** 15% of payment API calls failing with DNS resolution errors. Revenue impact.  
**Existing SentinelAI evidence:** Splunk: NXDOMAIN errors for `payments.processor.com`. Dynatrace: elevated outbound call failures. No infrastructure changes.  
**ThousandEyes evidence:** DNS test: `payments.processor.com` returning SERVFAIL from all agents. DNS time: 0ms (not resolving at all). Alert fired 3 minutes before app errors started.  
**Final RCA:** Authoritative DNS for payments processor failed; TE alerted 3 minutes before app observed impact.  
**Recommended owner:** Payments processor vendor (external) + internal DNS team to add secondary resolver  
**Confidence change:** 45 → 88 (+43)  
**Memory stored:** "DNS failure precedes payment API failures by 3 minutes" — add DNS health to pre-incident watchlist  

---

### UC-03: Online Banking Portal — CDN Edge Failure (EU Customers)

**Incident title:** Online banking unavailable for German customers  
**Symptom:** German customers cannot log in. UK and US customers unaffected.  
**Existing SentinelAI evidence:** Dynatrace: no errors. Kubernetes: pods healthy. Splunk: no app errors.  
**ThousandEyes evidence:** Frankfurt cloud agents report 0% availability on page-load test. London and US agents: 100%. Page-load waterfall: CDN edge in FRA returning 503. Other CDN pops: healthy.  
**Final RCA:** Cloudflare Frankfurt edge pop failure. Traffic failing to fall back correctly.  
**Recommended owner:** CDN vendor (Cloudflare) + platform team (fix failover configuration)  
**Confidence change:** 25 → 78 (+53)  
**Memory stored:** "Frankfurt CDN pop failure — 2nd occurrence this quarter; pattern: FRA1 instability during EU peak"  

---

### UC-04: Mobile Banking App — VPN Users Impacted Only

**Incident title:** Mobile banking slow for remote workers  
**Symptom:** Employees accessing banking app via corporate VPN report 10–30s load times. Direct internet users: normal.  
**Existing SentinelAI evidence:** App metrics: normal. Dynatrace: no latency. Splunk: no errors.  
**ThousandEyes evidence:** Enterprise VPN agents show 280ms latency (normal: 15ms). VPN tunnel packet loss: 12%. Cloud agents: normal.  
**Final RCA:** Corporate VPN concentrator overloaded; split tunneling misconfiguration routing all banking traffic through VPN.  
**Recommended owner:** IT/Network team (VPN infrastructure)  
**Confidence change:** 20 → 74 (+54)  
**Memory stored:** "VPN performance issues affect remote workers — isolate from P1 for external app"; pattern: VPN-only degradation  

---

### UC-05: Wire Transfer API — SaaS Intermediary Outage

**Incident title:** Wire transfer failures — SWIFT integration timeout  
**Symptom:** Wire transfers failing. SWIFT API calls timing out.  
**Existing SentinelAI evidence:** Splunk: `connection timeout` for `api.swift-integration-provider.com`. No internal changes.  
**ThousandEyes evidence:** HTTP test to `api.swift-integration-provider.com`: 0% availability from 6 cloud agents across US, EU, APAC. Internal services: all healthy.  
**Final RCA:** SWIFT integration provider experiencing global outage. Not our infrastructure.  
**Recommended owner:** SWIFT integration vendor — escalate immediately  
**Confidence change:** 55 → 95 (+40)  
**Memory stored:** "SWIFT provider outage — 2nd incident; add circuit breaker and fallback to secondary provider"  

---

## Hybrid Cloud / Kubernetes

---

### UC-06: Kubernetes Ingress — External Failure, Internal Healthy

**Incident title:** API gateway unavailable externally — all pods running  
**Symptom:** External API calls failing with connection refused. All internal checks green.  
**Existing SentinelAI evidence:** Sysdig: all pods Running, 0 restarts. Dynatrace: internal p99 normal. Splunk: no errors.  
**ThousandEyes evidence:** HTTP test from 4 cloud agents: 0% availability. Error: CONNECT_TIMEOUT. Enterprise agents inside VPC: 100% availability.  
**Final RCA:** Kubernetes LoadBalancer external IP lost its external route; external traffic cannot reach the load balancer IP.  
**Recommended owner:** Platform/infra team (LoadBalancer cloud provider)  
**Confidence change:** 30 → 85 (+55)  
**Memory stored:** "External connectivity failure without pod failures — check LB and ingress annotation"; pattern: external vs internal split  

---

### UC-07: Multi-Cluster Failover — DNS Routing Failure

**Incident title:** Traffic not routing to DR cluster after primary failure  
**Symptom:** Primary cluster failed over correctly. Users still hitting failed primary. DR cluster idle.  
**Existing SentinelAI evidence:** Kubernetes: primary cluster unhealthy. ServiceNow: planned failover completed.  
**ThousandEyes evidence:** DNS test: `api.example.com` still resolving to primary cluster IP (old A record). DNS propagation: only 20% of resolvers updated. TTL was set to 3600s.  
**Final RCA:** High DNS TTL (3600s) delayed failover DNS propagation. 80% of resolvers still using stale record.  
**Recommended owner:** Platform team — reduce DNS TTL for critical endpoints to 60s or less  
**Confidence change:** 50 → 88 (+38)  
**Memory stored:** "High DNS TTL blocks failover; change `api.example.com` TTL to 60s before next DR test"  

---

### UC-08: Service Mesh Latency — External vs. Internal Path

**Incident title:** Checkout service latency spike — unclear cause  
**Symptom:** Checkout service latency 4× normal. Users abandoning carts.  
**Existing SentinelAI evidence:** Dynatrace: checkout service latency 2400ms (baseline: 600ms). Internal microservice calls normal.  
**ThousandEyes evidence:** HTTP test: `connect_time` = 15ms (normal), `ttfb_ms` = 2200ms (baseline: 450ms). Network path clean.  
**Final RCA:** Wait time (TTFB) elevated means the app is slow processing the request, not the network. ThousandEyes network is clean — redirects investigation to app layer.  
**Recommended owner:** App team (checkout service performance)  
**Confidence change:** 40 → 70 (+30)  
**Memory stored:** "TE confirmed network healthy — checkout latency is app processing. DB query or downstream microservice."  

---

### UC-09: Kubernetes NodePort — Intermittent Packet Loss

**Incident title:** Intermittent API failures — every 5 minutes, 30-second windows  
**Symptom:** API intermittently unavailable. Pattern: 30 seconds of failures every 5 minutes.  
**Existing SentinelAI evidence:** Sysdig: pods healthy. No pattern in app logs.  
**ThousandEyes evidence:** Network test: 8–15% packet loss recurring every 5 minutes from 3 agents. Path vis: packet loss at hop 3 (transit ISP). Correlates exactly with failure windows.  
**Final RCA:** Transit ISP congestion buffer overflowing at 5-minute intervals (policing algorithm).  
**Recommended owner:** ISP (carrier policing/QoS issue)  
**Confidence change:** 25 → 75 (+50)  
**Memory stored:** "Intermittent ISP policing — recurring pattern on this transit link. Negotiate with carrier."  

---

### UC-10: Ingress Rate Limiting — Legitimate Traffic Blocked

**Incident title:** Legitimate users blocked — appears as DDoS  
**Symptom:** Large volume of 429 responses to legitimate users. App rate limiter incorrectly triggered.  
**Existing SentinelAI evidence:** Splunk: high 429 rate. Dynatrace: traffic spike. Appears to be DDoS.  
**ThousandEyes evidence:** HTTP test: responses are 429 with `X-Rate-Limit-Remaining: 0`. Traffic coming from legitimate IPs (no botnet ASN pattern). Agent-to-agent: network clean.  
**Final RCA:** CDN not correctly propagating real client IPs (X-Forwarded-For misconfiguration); rate limiter seeing all traffic as same IP.  
**Recommended owner:** Platform team (CDN/LB configuration)  
**Confidence change:** 45 → 80 (+35)  
**Memory stored:** "X-Forwarded-For misconfiguration causes incorrect rate limiting — verify on each CDN config change"  

---

## DNS

---

### UC-11: Authoritative DNS DNSSEC Failure

**Incident title:** Sporadic app unavailability — DNSSEC validation errors  
**Symptom:** Random 10–30% of users cannot reach app. Others unaffected. No pattern by geography.  
**Existing SentinelAI evidence:** Splunk: sparse errors. Dynatrace: no issues. Hard to reproduce.  
**ThousandEyes evidence:** DNS test: DNSSEC validation failing from 40% of resolvers. Different resolvers failing at different times. Root cause: DNSSEC record chain break.  
**Final RCA:** DNSSEC RRSIG record expired; resolvers with strict validation fail; others succeed (explains random 40%).  
**Recommended owner:** DNS team — DNSSEC operational process failure  
**Confidence change:** 20 → 82 (+62)  
**Memory stored:** "DNSSEC expiry caused partial outage — add RRSIG expiry monitoring to DNS observability"  

---

### UC-12: DNS Resolver Slowdown During Traffic Peak

**Incident title:** App latency spike at 09:00 every weekday  
**Symptom:** Every workday morning, 09:00–09:15, latency doubles.  
**Existing SentinelAI evidence:** Pattern detected. No infrastructure changes. App code unchanged.  
**ThousandEyes evidence:** DNS test: `dns_time_ms` spikes from 8ms to 650ms at exactly 09:00–09:15. Network path clean. TCP connect time normal (after DNS resolves).  
**Final RCA:** Internal DNS resolver overloaded by morning connection storm. Adding 640ms to every new connection.  
**Recommended owner:** Internal DNS/infrastructure team  
**Confidence change:** 50 → 87 (+37)  
**Memory stored:** Pattern: "Morning DNS storm — resolver needs capacity or caching improvement"; first_seen=2026-06-10  

---

## Internet-Facing Applications

---

### UC-13: SaaS Dependency — Stripe Payment Gateway

**Incident title:** Checkout failing — Stripe API unavailable  
**Symptom:** Checkout page failing with generic "payment error".  
**Existing SentinelAI evidence:** Splunk: `HTTP 503` from `api.stripe.com`. No code changes.  
**ThousandEyes evidence:** HTTP test to `api.stripe.com`: 0% availability from 5 cloud agents in US, EU, APAC. Error: `CONNECT_TIMEOUT`. Stripe status page: degraded.  
**Final RCA:** Stripe global infrastructure outage. Confirmed by TE multi-region evidence.  
**Recommended owner:** Stripe (external) — activate fallback payment processor  
**Confidence change:** 60 → 98 (+38)  
**Memory stored:** "Stripe outage confirmed by TE global agents. Activate PayPal fallback immediately."  

---

### UC-14: BGP Hijack — Traffic Rerouted Through Unexpected AS

**Incident title:** Unexplained 8× latency increase — no changes deployed  
**Symptom:** Global p99 latency increased 8×. Users from all regions affected.  
**Existing SentinelAI evidence:** No deployments. No infrastructure changes. Splunk: no errors. Dynatrace: high wait time globally.  
**ThousandEyes evidence:** BGP test: prefix `203.x.x.0/24` origin changed from AS16509 (Amazon) to AS9002 (RETN). Path length increased from 3 to 14 hops. BGP alert fired at incident start time exactly.  
**Final RCA:** BGP route leak — traffic rerouted through Eastern European transit provider adding massive latency.  
**Recommended owner:** AWS/Cloud provider networking team; open NOC ticket  
**Confidence change:** 30 → 90 (+60)  
**Memory stored:** "BGP route leak pattern — add BGP monitoring to all public prefixes; alert on origin AS change"  

---

### UC-15: Anycast CDN — Single Pop Overloaded

**Incident title:** Latency spike for US-West users only  
**Symptom:** US-West Coast users experiencing 3× normal load times. US-East fine.  
**Existing SentinelAI evidence:** Dynatrace: US-West origin requests increased (CDN cache miss spike). No West-specific deployments.  
**ThousandEyes evidence:** Page-load test: San Jose and Seattle agents show CDN edge returning 500ms TTFB. New York agents: 45ms TTFB. Path vis: West agents routing to different CDN IP than East.  
**Final RCA:** CDN West pop overloaded (CPU/memory capacity); traffic not failing over to healthy pop.  
**Recommended owner:** CDN vendor — emergency capacity + failover  
**Confidence change:** 45 → 82 (+37)  
**Memory stored:** "CDN West pop capacity pattern — third occurrence; evaluate CDN vendor failover policy"  

---

## VPN / Endpoint

---

### UC-16: Remote Worker Productivity — VPN Split Tunnel Misconfiguration

**Incident title:** Remote workers report app slow since VPN policy change  
**Symptom:** All apps slow for remote workers after IT pushed new VPN policy. In-office users fine.  
**Existing SentinelAI evidence:** App: healthy. Network team: "VPN is working."  
**ThousandEyes evidence:** Enterprise endpoint agents on VPN: 850ms TTFB. Off-VPN test (same location): 85ms TTFB. VPN tunnel RTT: 45ms. Gateway RTT: 2ms. Issue is in routing, not tunnel.  
**Final RCA:** VPN split-tunnel policy update routing all SaaS traffic through VPN instead of direct internet. VPN appliance adding 765ms due to inspection.  
**Recommended owner:** IT/Network team — update split-tunnel rules  
**Confidence change:** 20 → 80 (+60)  
**Memory stored:** "VPN policy change caused SaaS routing change — add TE endpoint test to change validation checklist"  

---

### UC-17: Office Wi-Fi Degradation — Regional Incident False Alarm

**Incident title:** Chicago office users reporting app failure — P1 raised  
**Symptom:** All users in Chicago reporting connectivity issues with internal app.  
**Existing SentinelAI evidence:** App: healthy. Splunk: no errors from Chicago. Appears to be app issue.  
**ThousandEyes evidence:** Enterprise endpoint agents in Chicago office: 0% availability. Cloud agents (US-Central): 100% availability. Gateway RTT at Chicago agents: 450ms (normal: 2ms).  
**Final RCA:** Chicago office network infrastructure (gateway/switch) failed. Not an app issue.  
**Recommended owner:** IT facilities team (Chicago office network)  
**Confidence change:** 15 → 72 (+57)  
**Memory stored:** "Chicago office network failure — third occurrence this year. Add redundant uplink."  

---

## Regional User Impact

---

### UC-18: Asia-Pacific Users — Peering Degradation

**Incident title:** APAC users report high latency — platform healthy  
**Symptom:** Singapore, Tokyo, Sydney users experiencing 4× normal latency. US/EU users fine.  
**Existing SentinelAI evidence:** Dynatrace: APAC service latency normal (APAC pods). Sysdig: APAC cluster healthy.  
**ThousandEyes evidence:** Singapore, Tokyo cloud agents: RTT to US origin 280ms (normal: 45ms). Path vis: APAC→US paths traversing AS3491 (PCCW) instead of direct peering. BGP: APAC prefix route changed.  
**Final RCA:** APAC peering route degraded; traffic falling back through PCCW suboptimal path.  
**Recommended owner:** Network/peering team — restore direct APAC peering  
**Confidence change:** 30 → 85 (+55)  
**Memory stored:** "APAC peering degradation → 4× latency via PCCW fallback. Ensure redundant APAC peering."  

---

### UC-19: Multi-Region Deployment — Partial DNS Cutover Failure

**Incident title:** EU deployment deployed but EU users still hitting US servers  
**Symptom:** EU datacenter deployed. EU users still seeing high latency (US response times).  
**Existing SentinelAI evidence:** ServiceNow: EU deployment change ticket marked complete. No evidence of why EU users hitting US.  
**ThousandEyes evidence:** DNS test from London, Frankfurt: `api.example.com` still resolving to US IP (44.x.x.x), not EU IP (52.x.x.x). DNS TTL: 300s. Last update: not propagated.  
**Final RCA:** DNS record for EU endpoint not updated as part of deployment. Change process failure.  
**Recommended owner:** Release engineering + DNS team  
**Confidence change:** 40 → 88 (+48)  
**Memory stored:** "DNS update missing from EU deployment runbook — add DNS verification step to deployment checklist"  

---

### UC-20: Black Friday Traffic — Regional CDN Capacity Exhaustion

**Incident title:** Checkout latency during peak sales event  
**Symptom:** Black Friday peak: checkout latency 5× normal for EU users. US users: normal.  
**Existing SentinelAI evidence:** Dynatrace: EU origin request rate 8× normal (CDN not absorbing). App metrics: memory pressure.  
**ThousandEyes evidence:** Page-load test EU agents: CDN TTFB elevated (800ms). Cache hit rate: 15% (normal: 85%). CDN pop capacity alert active for EU-West region.  
**Final RCA:** CDN EU-West capacity insufficient for Black Friday traffic. Cache miss storm overloading EU origin.  
**Recommended owner:** Platform team + CDN vendor (pre-provisioning failure)  
**Confidence change:** 55 → 87 (+32)  
**Memory stored:** "CDN EU capacity insufficient for peak traffic — reserve CDN capacity 48h before major events"  

---

## Additional Use Cases

---

### UC-21: SSL Certificate Expiry — Intermittent TLS Failures

**Incident title:** Intermittent TLS errors affecting 8% of users  
**Symptom:** Random TLS handshake failures. Hard to reproduce.  
**Existing evidence:** Splunk: `SSL_ERROR_RX_RECORD_TOO_LONG` errors. Dynatrace: spike in SSL errors.  
**ThousandEyes evidence:** HTTP test: `sslTime` elevated from 45ms to 3200ms on 8% of rounds. `errorType: SSL_HANDSHAKE_FAILURE` on those rounds. Path vis: different backend IP on failing rounds.  
**Final RCA:** One backend pod has expired certificate; load balancer routing 8% of requests to it.  
**Owner:** Platform team (certificate rotation)  
**Confidence change:** 45 → 86 (+41)  

---

### UC-22: IPv6 Transition Issues — Dual-Stack Failure

**Incident title:** Connectivity issues for ISPs transitioning to IPv6  
**Symptom:** 5% of users (specific ISPs) failing. No pattern in server logs.  
**ThousandEyes evidence:** IPv6-enabled cloud agents showing connection failures. IPv4-only agents: healthy. Specific IPv6 path drops at AS6830 (Liberty Global).  
**Final RCA:** IPv6 routing table incomplete at Liberty Global — dual-stack fallback not working correctly.  
**Owner:** Network team — Liberty Global escalation  
**Confidence change:** 25 → 78 (+53)  

---

### UC-23: Microservice Dependency — External API Rate Limiting

**Incident title:** Internal service degraded — external enrichment API throttled  
**Symptom:** Product recommendations service slow. External recommendation API being throttled.  
**ThousandEyes evidence:** HTTP test to enrichment API: 100% availability but `responseCode=429`. TTFB elevated due to retry logic.  
**Final RCA:** Enrichment API rate limit exhausted. Internal retry loop amplifying impact.  
**Owner:** App team (rate limit handling + exponential backoff)  
**Confidence change:** 60 → 88 (+28)  

---

### UC-24: Containerized App — Intermittent 503 from Kubernetes Ingress

**Incident title:** Random 503 errors from ingress — no pod failures  
**Symptom:** 2% of requests returning 503. Pods healthy. Load balancer healthy.  
**ThousandEyes evidence:** HTTP test: `responseCode=503` on 2% of rounds. Error: `upstream timed out`. TE timing: `connectTime` normal, `waitTime` normal, 503 occurs at TLS termination.  
**Final RCA:** Nginx ingress `proxy_read_timeout` too short for slow backend responses.  
**Owner:** Platform team (ingress configuration)  
**Confidence change:** 40 → 76 (+36)  

---

### UC-25: Multi-Cloud DR — Failover Latency Unacceptable

**Incident title:** DR failover to GCP succeeded but latency unacceptable  
**Symptom:** DR test: failover to GCP working but users experiencing 8× normal latency from GCP.  
**ThousandEyes evidence:** HTTP test pointing to GCP endpoints: 100% availability but `response_time_ms` = 4× AWS baseline. Path vis: GCP egress routing through different ISP with 4× more hops.  
**Final RCA:** GCP egress uses different peering than AWS for user-facing traffic. DR plan must account for network path, not just compute availability.  
**Owner:** Architecture team (DR network design)  
**Confidence change:** 50 → 80 (+30)  
**Memory stored:** "DR failover succeeded but network path unacceptable — add network latency to DR acceptance criteria"  

---

## Summary: Top 10 Use Cases by Confidence Gain

| Rank | Use Case | Confidence Gain | Category |
|------|----------|----------------|----------|
| 1 | Kubernetes Ingress Failure (UC-06) | +55 | Hybrid Cloud |
| 2 | BGP Hijack (UC-14) | +60 | Internet |
| 3 | VPN Split Tunnel (UC-16) | +60 | VPN/Endpoint |
| 4 | Chicago Wi-Fi False Alarm (UC-17) | +57 | Endpoint |
| 5 | Trading Platform BGP (UC-01) | +52 | Financial |
| 6 | DNSSEC Failure (UC-11) | +62 | DNS |
| 7 | Frankfurt CDN (UC-03) | +53 | Financial/CDN |
| 8 | IPv6 Transition (UC-22) | +53 | Internet |
| 9 | APAC Peering (UC-18) | +55 | Regional |
| 10 | DNS + Payment API (UC-02) | +43 | Financial |
