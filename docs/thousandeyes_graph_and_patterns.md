# ThousandEyes Knowledge Graph and Pattern Intelligence

**Date:** 2026-06-10  
**Purpose:** How ThousandEyes enriches SentinelAI's incident graph and operational memory  

---

## New Graph Node Types

The existing `intelligence/incident_graph.py` supports 9 node types. ThousandEyes introduces 4 new types:

| New Node Type | Description | Key Properties |
|--------------|-------------|----------------|
| `te_test` | A configured ThousandEyes test | test_id, test_name, test_type, target |
| `agent_location` | A ThousandEyes agent (cloud, enterprise, endpoint) | agent_id, location, region, agent_type, asn |
| `network_path` | A recorded path visualization snapshot | hop_count, changed_hops, path_fingerprint |
| `asn_provider` | An Autonomous System / ISP/carrier | asn, provider_name, country |

Existing node types that ThousandEyes evidence maps to:
- `alert` — ThousandEyes alert (already exists; TE alerts add `source: "thousandeyes"`)
- `service` — the service being monitored by a TE test
- `outcome` — root cause conclusion (shared with app evidence)

---

## New Graph Edge Types

The existing `EdgeRelationship` enum has 8 types. ThousandEyes introduces 8 new typed relationships:

| Edge | Source Node | Target Node | Meaning |
|------|------------|------------|---------|
| `INCIDENT_OBSERVED_BY_TEST` | Incident | `te_test` | This TE test was active during the incident |
| `TEST_TARGETS_SERVICE` | `te_test` | `service` | This test monitors this service |
| `TEST_RAN_FROM_LOCATION` | `te_test` | `agent_location` | Test ran from this agent during the incident |
| `PATH_TRAVERSED_ASN` | `network_path` | `asn_provider` | The path went through this AS |
| `ALERT_OVERLAPS_INCIDENT` | `alert` (TE) | `incident` | TE alert time window overlaps incident window |
| `PROVIDER_DEGRADED_DURING_INCIDENT` | `asn_provider` | `incident` | Provider's AS showed degradation during this incident |
| `DNS_RESOLVER_FAILED` | `agent_location` | `service` | DNS resolution to service failed from this agent |
| `ENDPOINT_IMPACTED_BY_NETWORK` | `agent_location` (endpoint) | `incident` | Endpoint agent observed degradation |

---

## Updated Schema in `intelligence/schema.py`

```python
class NodeType(str, Enum):
    # Existing
    METRIC   = "metric"
    LOG      = "log"
    EVENT    = "event"
    CHANGE   = "change"
    TRACE    = "trace"
    ALERT    = "alert"
    CMDB     = "cmdb"
    RUNBOOK  = "runbook"
    OUTCOME  = "outcome"
    # New (ThousandEyes)
    TE_TEST         = "te_test"
    AGENT_LOCATION  = "agent_location"
    NETWORK_PATH    = "network_path"
    ASN_PROVIDER    = "asn_provider"

class EdgeRelationship(str, Enum):
    # Existing
    CAUSED_BY    = "CAUSED_BY"
    PRECEDED     = "PRECEDED"
    CORRELATED   = "CORRELATED"
    AFFECTS      = "AFFECTS"
    RUNS_ON      = "RUNS_ON"
    HOSTED_ON    = "HOSTED_ON"
    DEPENDS_ON   = "DEPENDS_ON"
    GENERATED_BY = "GENERATED_BY"
    # New (ThousandEyes)
    INCIDENT_OBSERVED_BY_TEST     = "INCIDENT_OBSERVED_BY_TEST"
    TEST_TARGETS_SERVICE          = "TEST_TARGETS_SERVICE"
    TEST_RAN_FROM_LOCATION        = "TEST_RAN_FROM_LOCATION"
    PATH_TRAVERSED_ASN            = "PATH_TRAVERSED_ASN"
    ALERT_OVERLAPS_INCIDENT       = "ALERT_OVERLAPS_INCIDENT"
    PROVIDER_DEGRADED_DURING      = "PROVIDER_DEGRADED_DURING"
    DNS_RESOLVER_FAILED           = "DNS_RESOLVER_FAILED"
    ENDPOINT_IMPACTED_BY_NETWORK  = "ENDPOINT_IMPACTED_BY_NETWORK"
```

---

## Sample Graph Subgraph (JSON)

```json
{
  "nodes": [
    {"node_id": "svc-api-01", "node_type": "service", "label": "api-gateway"},
    {"node_id": "te-test-123456", "node_type": "te_test", 
     "label": "API Gateway Health", "properties": {"test_type": "http-server", "target": "https://api.example.com/health"}},
    {"node_id": "agent-ny-10002", "node_type": "agent_location", 
     "label": "New York, NY", "properties": {"agent_type": "cloud", "region": "us-east"}},
    {"node_id": "asn-7922", "node_type": "asn_provider", 
     "label": "Comcast AS7922", "properties": {"asn": "AS7922", "provider_name": "Comcast"}},
    {"node_id": "path-snap-abc", "node_type": "network_path", 
     "label": "NY→API path 2026-06-10T10:00Z", "properties": {"hop_count": 8, "changed_hops": 2}},
    {"node_id": "alert-te-987654", "node_type": "alert", 
     "label": "API Gateway Availability < 90%", "properties": {"source": "thousandeyes", "severity": "CRITICAL"}}
  ],
  "edges": [
    {"edge_id": "e1", "source": "inc-001", "target": "te-test-123456", "relationship": "INCIDENT_OBSERVED_BY_TEST"},
    {"edge_id": "e2", "source": "te-test-123456", "target": "svc-api-01", "relationship": "TEST_TARGETS_SERVICE"},
    {"edge_id": "e3", "source": "te-test-123456", "target": "agent-ny-10002", "relationship": "TEST_RAN_FROM_LOCATION"},
    {"edge_id": "e4", "source": "path-snap-abc", "target": "asn-7922", "relationship": "PATH_TRAVERSED_ASN"},
    {"edge_id": "e5", "source": "asn-7922", "target": "inc-001", "relationship": "PROVIDER_DEGRADED_DURING"},
    {"edge_id": "e6", "source": "alert-te-987654", "target": "inc-001", "relationship": "ALERT_OVERLAPS_INCIDENT"}
  ]
}
```

---

## Pattern Intelligence: New Recurring Patterns

The existing `PatternIntelligenceStore` detects patterns by symptom signature. ThousandEyes enables these new pattern classes:

### Pattern Class 1: Recurring ISP Degradation

**Signature tokens:** `["isp_degradation", "{asn}", "{provider_name}", "{region}"]`  
**Detection:** Same ASN appears in `PATH_TRAVERSED_ASN` edges for ≥3 distinct incidents  
**Operational value:** "Comcast in New York degrades 3x per month, always between 18:00–20:00"  
**Memory stored:** `OperationalPattern` with `incident_type="isp_degradation"`, `services=[affected_services]`  
**Recommended action template:** "Open standing case with Comcast NOC for AS7922 congestion window"

---

### Pattern Class 2: Recurring DNS Latency

**Signature tokens:** `["dns_latency", "{dns_resolver}", "{target_domain}"]`  
**Detection:** DNS test shows elevated `dns_time_ms` in ≥3 incidents for same resolver  
**Operational value:** "DNS resolver 8.x.x.x adds 800ms for *.example.com every Monday morning"  
**Memory stored:** `OperationalPattern` + `ServiceDependency` edge (service → dns_provider)  
**Recommended action template:** "Switch to secondary DNS resolver; investigate primary resolver capacity"

---

### Pattern Class 3: Recurring SaaS Outage

**Signature tokens:** `["saas_outage", "{saas_domain}"]`  
**Detection:** HTTP test to SaaS shows availability=0% in ≥2 incidents  
**Operational value:** "Salesforce has had 2 outages in 90 days; implement circuit breaker"  
**Memory stored:** `OperationalPattern` + `ChangeImpactLink` (saas outage → incident)

---

### Pattern Class 4: Recurring Regional Packet Loss

**Signature tokens:** `["packet_loss", "{region}", "{agent_location}"]`  
**Detection:** Packet loss >5% from same region in ≥3 incidents  
**Operational value:** "EU-West agents show recurring packet loss every Tuesday afternoon"  
**Memory stored:** `OperationalPattern` with affected region; `ServiceDependency` with dep_type="network"

---

### Pattern Class 5: Recurring VPN / Endpoint Issues

**Signature tokens:** `["vpn_degradation", "{office_location}"]`  
**Detection:** Enterprise endpoint agents at same location degrade in ≥3 incidents  
**Operational value:** "London office VPN tunnel shows recurring packet loss on Mondays"  
**Memory stored:** `OperationalPattern` with `incident_type="vpn_degradation"`, services=[]

---

### Pattern Class 6: Internal Clean / External Dirty

**Signature tokens:** `["internal_clean_external_dirty", "{service}"]`  
**Detection:** Internal APM shows healthy + ThousandEyes external tests fail, occurring in ≥2 incidents  
**Operational value:** "API gateway reports clean internally but external users fail — likely ingress/CDN"  
**Memory stored:** `ResolutionMemory` with `lesson_learned="Internal health != external health; add external synthetic monitoring"`

---

### Pattern Class 7: CDN Pop Failure (Regional)

**Signature tokens:** `["cdn_failure", "{cdn_region}", "{cdn_provider}"]`  
**Detection:** Page-load tests from specific region fail due to CDN edge returning 5xx, ≥2 incidents  
**Operational value:** "Cloudflare FRA1 (Frankfurt) pop fails during European peak hours"  
**Memory stored:** `OperationalPattern` + `ServiceDependency` (service → cdn_provider, dep_type="cdn")

---

## Intel Writer Integration

When `ENABLE_THOUSANDEYES_RCA=true` and ThousandEyes evidence is collected, `intel_writer.capture()` is extended to also:

```python
def _capture_network_graph(investigation_id, incident_id, service, te_evidence):
    """Add ThousandEyes evidence as typed nodes in the incident graph."""
    store = IncidentGraphStore(_DB_PATH)
    
    for ev in te_evidence:
        # TE Test node
        test_node = store.make_node("te_test", ev["test_name"], incident_id, 
                                    service=service, properties=ev)
        store.add_node(test_node)
        
        # Agent location node
        agent_node = store.make_node("agent_location", ev["agent_location"], incident_id,
                                     properties={"region": ev["region"], "asn": ev["asn"]})
        store.add_node(agent_node)
        
        # ASN/provider node (if degraded)
        if ev.get("recommended_owner") in ("isp", "network") and ev.get("asn"):
            asn_node = store.make_node("asn_provider", ev["asn"], incident_id,
                                       properties={"provider": ev.get("provider")})
            store.add_node(asn_node)
            store.add_edge(store.make_edge(asn_node.node_id, test_node.node_id, 
                                           "PROVIDER_DEGRADED_DURING", incident_id))
        
        # Test → agent edge
        store.add_edge(store.make_edge(test_node.node_id, agent_node.node_id,
                                       "TEST_RAN_FROM_LOCATION", incident_id))
```

---

## Cross-Investigation Pattern Accumulation

Over time, the `operational_patterns` SQLite table accumulates network-induced patterns:

```sql
-- Query: Which services have recurring ISP-related incidents?
SELECT 
    p.canonical_symptoms,
    p.incident_type,
    p.occurrence_count,
    p.success_rate,
    p.last_seen
FROM operational_patterns p
WHERE p.canonical_symptoms LIKE '%isp_degradation%'
   OR p.canonical_symptoms LIKE '%packet_loss%'
   OR p.canonical_symptoms LIKE '%dns_latency%'
ORDER BY p.occurrence_count DESC
LIMIT 20;
```

This enables SentinelAI to surface: "This service has experienced ISP-related degradation 4 times in the past 90 days. Consider adding redundant peering or CDN caching."
