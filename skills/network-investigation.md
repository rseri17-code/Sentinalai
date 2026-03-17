---
skill: network-investigation
description: >
  Investigate network connectivity failures: DNS resolution failures, TLS
  certificate issues, connection refused, ECONNREFUSED, socket timeout.
  Use for: connectivity, network unreachable, certificate expired incidents.
playbook: network
incident_types: [network]
max_calls: 6
---

# Network Investigation Skill

## When to Use

Activate when the incident summary contains: `connectivity`, `connection refused`,
`dns`, `network`, `econnrefused`, `socket timeout`, `network unreachable`,
`certificate`, `tls`, `ssl`.

## Investigation Steps (ordered)

### Step 1 — Fetch Incident (`ops_worker.get_incident_by_id`)

### Step 2 — Search Network Logs (`log_worker.search_logs`)
Query: `connection refused OR dns {service}`
Look for: ECONNREFUSED, ETIMEDOUT, DNS NXDOMAIN, TLS handshake failure,
certificate verification errors, host unreachable.

### Step 3 — Check Golden Signals (`apm_worker.get_golden_signals`)
Look for: error rate spike with latency not elevated (connection failure
pattern), which downstream service is failing, traffic pattern changes.

### Step 4 — Search DNS Logs (`log_worker.search_logs`)
Query: `dns {service}`
Look for: resolution failures, SERVFAIL, changed DNS records, split-horizon
DNS issues.

### Step 5 — Check Changes (`log_worker.get_change_data`)
Look for: network policy changes, service mesh config updates, certificate
rotation, DNS record modifications, firewall rule changes.

### Step 6 — Check ITSM Changes (`itsm_worker.get_change_records`)
Look for: infrastructure change records for networking components,
firewall changes, load balancer updates.

## Hypotheses to Score

| Hypothesis              | Key evidence signal                                      |
|-------------------------|----------------------------------------------------------|
| dns_resolution_failure  | DNS NXDOMAIN or SERVFAIL in logs                        |
| certificate_expired     | TLS handshake failure with cert expiry message          |
| firewall_rule_change    | ITSM change record for network policy                   |
| service_mesh_misconfiguration | Service mesh config change + connection refused  |
| network_partition       | Multiple services losing connectivity simultaneously    |
| port_not_listening      | Connection refused to specific port, no service restart |

## Success Criteria

- Network layer of failure identified (DNS vs TLS vs routing vs firewall)
- Specific error code captured from logs
- Change correlation attempted for network/infra layer
