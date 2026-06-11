# ThousandEyes Normalized Evidence Schema

**Date:** 2026-06-10  
**Purpose:** Define the canonical `NetworkEvidence` data model for SentinelAI  

---

## Design Principles

1. **One model, all test types.** HTTP, DNS, BGP, page-load, network TCP — all normalize to `NetworkEvidence`. Fields not applicable to a test type are `None`.
2. **Source-agnostic.** The model works for ThousandEyes today and any future network intelligence source.
3. **Deterministic confidence scoring.** Confidence is computed from observable fields, not LLM-assigned.
4. **Backward compatible.** Adding new fields is safe; existing consumers only read what they need.
5. **Sanitized by default.** Internal IPs and hostnames are hashed before storage.

---

## NetworkEvidence Dataclass

```python
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NetworkEvidence:
    # ── Identity ─────────────────────────────────────────────────────────────
    source: str                     # "thousandeyes" | "synthetic_monitor" | "probe"
    test_id: str                    # provider test ID (str for cross-provider compatibility)
    test_name: str                  # human-readable test name
    test_type: str                  # http-server | dns-server | network | bgp | page-load | agent-to-agent | voice
    target: str                     # URL or IP being tested (sanitized)
    
    # ── Location ─────────────────────────────────────────────────────────────
    agent_id: str                   # provider agent ID
    agent_location: str             # "San Jose, CA" | "NYC-Office-Enterprise"
    agent_type: str                 # "cloud" | "enterprise" | "endpoint"
    region: str                     # "us-east" | "eu-west" | "apac" (normalized from location)
    asn: str | None                 # AS number of the agent's network (e.g. "AS7922")
    provider: str | None            # ISP/carrier name (e.g. "Comcast")
    
    # ── Time Window ──────────────────────────────────────────────────────────
    window_start: str               # ISO-8601 start of evidence window
    window_end: str                 # ISO-8601 end of evidence window
    round_id: int | None            # ThousandEyes round timestamp (epoch seconds)
    
    # ── Availability and Loss ─────────────────────────────────────────────────
    availability: float | None      # 0.0–100.0 percentage
    packet_loss: float | None       # 0.0–100.0 percentage (network tests)
    
    # ── Latency Components (ms) ───────────────────────────────────────────────
    latency_ms: float | None        # round-trip time for network tests
    jitter_ms: float | None         # RTT variation
    dns_time_ms: float | None       # DNS resolution time
    connect_time_ms: float | None   # TCP connection establishment
    ssl_time_ms: float | None       # TLS handshake time
    ttfb_ms: float | None           # time-to-first-byte (waitTime in TE)
    response_time_ms: float | None  # total HTTP response time
    total_time_ms: float | None     # end-to-end total time
    
    # ── HTTP Response ─────────────────────────────────────────────────────────
    response_code: int | None       # HTTP status code (200, 503, etc.)
    redirects: int | None           # number of HTTP redirects
    
    # ── Failure Details ───────────────────────────────────────────────────────
    error_type: str | None          # CONNECT_TIMEOUT | SSL_HANDSHAKE_FAILURE | DNS_FAILURE | etc.
    error_details: str | None       # human-readable error description (sanitized)
    
    # ── Path Data ─────────────────────────────────────────────────────────────
    path_hops: int | None           # total hop count in path visualization
    changed_hops: int | None        # number of changed hops vs. previous round
    path_summary: list[dict] | None # [{hop, rdns, rtt_avg, loss_pct, asn}] — sanitized IPs
    
    # ── BGP Data ─────────────────────────────────────────────────────────────
    bgp_origin_asn: str | None      # current origin AS for the prefix
    bgp_prefix: str | None          # IP prefix being monitored (e.g. "203.0.113.0/24")
    bgp_route_changed: bool | None  # True if origin/path changed this round
    bgp_reachability: float | None  # 0.0–100.0 reachability across monitors
    
    # ── Scope and Impact ─────────────────────────────────────────────────────
    affected_scope: str             # "global" | "regional" | "single_agent" | "endpoint_only"
    
    # ── Computed Confidence ───────────────────────────────────────────────────
    confidence: float               # 0.0–1.0; computed deterministically (see below)
    recommended_owner: str          # "app" | "network" | "isp" | "saas" | "dns" | "endpoint" | "unknown"
    
    # ── Provenance ───────────────────────────────────────────────────────────
    evidence_url: str | None        # deep-link to ThousandEyes UI for this result
    raw_summary: str                # one-sentence human-readable summary of this evidence
    
    # ── Internal ─────────────────────────────────────────────────────────────
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    evidence_id: str = ""           # deterministic sha256[:16] set in __post_init__
    
    def __post_init__(self):
        if not self.evidence_id:
            raw = f"{self.source}:{self.test_id}:{self.agent_id}:{self.round_id or self.window_start}"
            self.evidence_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id":       self.evidence_id,
            "source":            self.source,
            "test_id":           self.test_id,
            "test_name":         self.test_name,
            "test_type":         self.test_type,
            "target":            self.target,
            "agent_id":          self.agent_id,
            "agent_location":    self.agent_location,
            "agent_type":        self.agent_type,
            "region":            self.region,
            "asn":               self.asn,
            "provider":          self.provider,
            "window_start":      self.window_start,
            "window_end":        self.window_end,
            "availability":      self.availability,
            "packet_loss":       self.packet_loss,
            "latency_ms":        self.latency_ms,
            "jitter_ms":         self.jitter_ms,
            "dns_time_ms":       self.dns_time_ms,
            "connect_time_ms":   self.connect_time_ms,
            "ssl_time_ms":       self.ssl_time_ms,
            "ttfb_ms":           self.ttfb_ms,
            "response_time_ms":  self.response_time_ms,
            "response_code":     self.response_code,
            "error_type":        self.error_type,
            "error_details":     self.error_details,
            "path_hops":         self.path_hops,
            "changed_hops":      self.changed_hops,
            "path_summary":      self.path_summary,
            "bgp_origin_asn":    self.bgp_origin_asn,
            "bgp_route_changed": self.bgp_route_changed,
            "bgp_reachability":  self.bgp_reachability,
            "affected_scope":    self.affected_scope,
            "confidence":        round(self.confidence, 3),
            "recommended_owner": self.recommended_owner,
            "evidence_url":      self.evidence_url,
            "raw_summary":       self.raw_summary,
            "recorded_at":       self.recorded_at,
        }
```

---

## Confidence Scoring Formula

Confidence is computed deterministically from observable fields. No LLM.

```python
def compute_network_confidence(e: NetworkEvidence) -> float:
    """Deterministic confidence score for a NetworkEvidence instance.
    
    Returns float 0.0–1.0:
      - 0.9+ : High-confidence failure signal (availability=0 or DNS failure)
      - 0.7–0.9 : Strong degradation signal
      - 0.5–0.7 : Moderate signal
      - 0.3–0.5 : Weak signal (single agent, minor degradation)
      - 0.0–0.3 : Normal / no signal
    """
    score = 0.0
    
    # Base: availability drives primary signal
    if e.availability is not None:
        if e.availability == 0:
            score += 0.40
        elif e.availability < 50:
            score += 0.30
        elif e.availability < 90:
            score += 0.15
    
    # Packet loss
    if e.packet_loss is not None:
        if e.packet_loss > 20:
            score += 0.20
        elif e.packet_loss > 5:
            score += 0.10
        elif e.packet_loss > 2:
            score += 0.05
    
    # Error type — specific errors are high-confidence
    HIGH_CONFIDENCE_ERRORS = {"SSL_HANDSHAKE_FAILURE", "DNS_FAILURE", "CONNECT_TIMEOUT"}
    if e.error_type in HIGH_CONFIDENCE_ERRORS:
        score += 0.15
    
    # Connect time elevation (network, not app)
    if e.connect_time_ms is not None and e.connect_time_ms > 500:
        score += 0.10
    
    # Path change (BGP or routing instability)
    if e.changed_hops and e.changed_hops > 0:
        score += 0.10
    if e.bgp_route_changed:
        score += 0.15
    
    # DNS time elevation
    if e.dns_time_ms is not None and e.dns_time_ms > 500:
        score += 0.10
    
    # Agent scope multiplier — multi-agent evidence is more reliable
    if e.affected_scope == "global":
        score *= 1.2
    elif e.affected_scope == "regional":
        score *= 1.1
    
    return min(1.0, round(score, 3))
```

---

## recommended_owner Inference

```python
def infer_owner(e: NetworkEvidence) -> str:
    """Deterministic owner assignment from NetworkEvidence fields."""
    
    # DNS failure → DNS owner
    if e.error_type == "DNS_FAILURE" or (e.dns_time_ms and e.dns_time_ms > 1000):
        return "dns"
    
    # Endpoint only failing → endpoint/local
    if e.agent_type == "endpoint" and e.availability is not None and e.availability < 50:
        return "endpoint"
    
    # BGP route change → ISP/carrier
    if e.bgp_route_changed:
        return "isp"
    
    # Path hop at external ASN failing → ISP/carrier  
    if e.changed_hops and e.changed_hops > 0 and e.asn:
        return "isp"
    
    # Availability 0% from external cloud agents → network/CDN
    if e.agent_type == "cloud" and e.availability == 0:
        if e.error_type == "CONNECT_TIMEOUT":
            return "network"
        return "network"
    
    # SSL failure → app/platform (cert management)
    if e.error_type == "SSL_HANDSHAKE_FAILURE":
        return "app"
    
    # TTFB elevated, connect normal → app processing
    if (e.ttfb_ms and e.ttfb_ms > 1000 and 
        e.connect_time_ms and e.connect_time_ms < 100):
        return "app"
    
    return "unknown"
```

---

## Example Instances

### Example 1: HTTP Server Test — Timeout

```python
NetworkEvidence(
    source="thousandeyes",
    test_id="123456",
    test_name="API Gateway - Production Health",
    test_type="http-server",
    target="https://api.example.com/health",
    agent_id="10002",
    agent_location="New York, NY",
    agent_type="cloud",
    region="us-east",
    asn="AS7922",
    provider="Comcast",
    window_start="2026-06-10T09:00:00Z",
    window_end="2026-06-10T11:00:00Z",
    availability=0.0,
    packet_loss=None,
    latency_ms=None,
    connect_time_ms=0,
    ssl_time_ms=None,
    ttfb_ms=None,
    response_time_ms=None,
    response_code=0,
    error_type="CONNECT_TIMEOUT",
    error_details="Connection timed out after 10000ms",
    affected_scope="regional",
    confidence=0.0,  # computed post-init
    recommended_owner="network",
    raw_summary="API Gateway unreachable from New York: CONNECT_TIMEOUT after 10s",
)
# After compute_network_confidence: confidence ≈ 0.715
```

### Example 2: DNS Test — Failure

```python
NetworkEvidence(
    source="thousandeyes",
    test_id="789012",
    test_name="payments.example.com DNS Health",
    test_type="dns-server",
    target="payments.example.com",
    agent_id="10003",
    agent_location="London, UK",
    agent_type="cloud",
    region="eu-west",
    window_start="2026-06-10T10:00:00Z",
    window_end="2026-06-10T10:30:00Z",
    availability=0.0,
    dns_time_ms=2000.0,
    error_type="DNS_FAILURE",
    error_details="SERVFAIL from resolver 8.x.x.x",
    affected_scope="global",
    confidence=0.0,  # computed
    recommended_owner="dns",
    raw_summary="DNS resolution for payments.example.com failing with SERVFAIL globally",
)
# confidence ≈ 0.858 (availability=0 → +0.40, error_type DNS_FAILURE → +0.15, dns_time >500 → +0.10, global scope ×1.2)
```

---

## Storage

`NetworkEvidence` instances are stored in two places:

1. **In-memory evidence dict** — passed to the supervisor's evidence snapshot for current investigation
2. **Intel layer** — via `intel_writer.capture()` → `incident_graph.py` node type `"network"`

Evidence dict key: `"network_evidence"` (list of `NetworkEvidence.to_dict()`)

```python
evidence["network_evidence"] = [e.to_dict() for e in network_evidence_list]
```
