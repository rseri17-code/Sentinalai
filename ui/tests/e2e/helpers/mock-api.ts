/**
 * Shared API mocks — intercepts all /api/* requests so tests run without a live backend.
 */
import { Page } from '@playwright/test'

export const MOCK_INVESTIGATIONS = [
  {
    investigation_id: 'inv-001',
    incident_id: 'INC12345',
    summary: 'API Gateway timeout spike',
    affected_service: 'payment-service',
    severity: 'critical',
    incident_type: 'timeout',
    source: 'moogsoft',
    status: 'completed',
    playbook: [],
    started_at: '2024-02-12T10:30:00Z',
    completed_at: '2024-02-12T10:31:00Z',
    duration_ms: 62000,
    root_cause: 'payment-service database slow queries causing upstream timeout',
    confidence: 0.87,
    risk_level: 'medium',
    hypotheses: [
      { name: 'DB overload', root_cause: 'Slow queries', score: 0.87, is_winner: true, evidence_refs: [] },
    ],
    winner_hypothesis: 'DB overload',
    receipt_ids: [],
    tool_calls_total: 12,
    tool_calls_success: 11,
    tool_calls_failed: 1,
    memory_matches: [],
    judge_scores: { root_cause_accuracy: 0.9, causal_reasoning: 0.85, evidence_usage: 0.88 },
    control_actions: [],
    data_freshness: {},
    stale_sources: [],
    replay_available: true,
    event_count: 45,
    budget_used: 8,
    budget_max: 20,
    schema_version: '1.0',
    trace_id: 'trace-001',
  },
  {
    investigation_id: 'inv-002',
    incident_id: 'INC12346',
    summary: 'user-service OOMKill',
    affected_service: 'user-service',
    severity: 'major',
    incident_type: 'oomkill',
    source: 'moogsoft',
    status: 'running',
    playbook: [],
    started_at: '2024-02-12T11:00:00Z',
    root_cause: '',
    confidence: 0.45,
    risk_level: 'high',
    hypotheses: [],
    receipt_ids: [],
    tool_calls_total: 5,
    tool_calls_success: 5,
    tool_calls_failed: 0,
    memory_matches: [],
    judge_scores: {},
    control_actions: [],
    data_freshness: {},
    stale_sources: [],
    replay_available: false,
    event_count: 12,
    budget_used: 4,
    budget_max: 20,
    schema_version: '1.0',
    trace_id: 'trace-002',
  },
]

export const MOCK_BLAST_RADIUS = {
  target_service: 'payment-service',
  fix_type: 'rollback',
  risk_tier: 'MEDIUM',
  safe_to_auto_apply: false,
  affected_service_count: 4,
  p1_dependency_count: 2,
  affected_services: [
    { service: 'checkout-service', risk_tier: 'HIGH', impact_score: 82, hop_distance: 1, downstream_count: 3, has_p1_dependency: true, precautions: ['Drain connections first'] },
    { service: 'order-service', risk_tier: 'MEDIUM', impact_score: 55, hop_distance: 2, downstream_count: 1, has_p1_dependency: true, precautions: [] },
    { service: 'notification-service', risk_tier: 'LOW', impact_score: 20, hop_distance: 3, downstream_count: 0, has_p1_dependency: false, precautions: [] },
    { service: 'analytics-service', risk_tier: 'LOW', impact_score: 15, hop_distance: 3, downstream_count: 0, has_p1_dependency: false, precautions: [] },
  ],
  precautions: ['Drain active connections before rolling back', 'Notify checkout team'],
  reasoning: 'Rollback will restart payment-service pods. checkout-service has sync dependency.',
  generated_at: new Date().toISOString(),
}

export const MOCK_INTELLIGENCE_FEED = {
  alerts: [
    {
      id: 'alert-001',
      service: 'payment-service',
      metric_name: 'cpu_utilisation',
      current_value: 87.3,
      threshold: 90.0,
      urgency: 'WARNING',
      trend_direction: 'rising',
      minutes_to_breach: 18.5,
      recommended_action: 'Scale horizontally by adding 2 replicas',
      confidence: 0.84,
      detected_at: new Date().toISOString(),
    },
    {
      id: 'alert-002',
      service: 'order-db',
      metric_name: 'connection_pool_utilisation',
      current_value: 94.1,
      threshold: 95.0,
      urgency: 'IMMINENT',
      trend_direction: 'rising',
      minutes_to_breach: 4.2,
      recommended_action: 'Increase connection pool size immediately',
      confidence: 0.91,
      detected_at: new Date().toISOString(),
    },
    {
      id: 'alert-003',
      service: 'cart-service',
      metric_name: 'memory_utilisation',
      current_value: 71.0,
      threshold: 85.0,
      urgency: 'WATCH',
      trend_direction: 'rising',
      minutes_to_breach: 45.0,
      recommended_action: 'Monitor memory growth',
      confidence: 0.72,
      detected_at: new Date().toISOString(),
    },
    {
      id: 'alert-004',
      service: 'search-service',
      metric_name: 'error_rate',
      current_value: 4.8,
      threshold: 5.0,
      urgency: 'BREACHED',
      trend_direction: 'rising',
      minutes_to_breach: 0,
      recommended_action: 'Investigate immediately — circuit breaker may trip',
      confidence: 0.97,
      detected_at: new Date().toISOString(),
    },
  ],
  generated_at: new Date().toISOString(),
}

export const MOCK_HANDOFF = {
  generated_at: new Date().toISOString(),
  outgoing_engineer: 'alice',
  incoming_engineer: 'bob',
  shift_start: new Date().toISOString(),
  summary: '3 fragile services detected, 1 active investigation, 2 deployments scheduled overnight.',
  fragile_services: [
    {
      service: 'payment-service',
      risk_level: 'critical' as const,
      incident_count_7d: 5,
      last_incident_type: 'timeout',
      reason: '5 incidents in 7 days, CPU trending high',
      watch_signals: ['CPU at 87%', 'P95 latency elevated'],
    },
    {
      service: 'order-db',
      risk_level: 'elevated' as const,
      incident_count_7d: 2,
      last_incident_type: 'error_spike',
      reason: 'Connection pool near saturation',
      watch_signals: ['Pool at 94%'],
    },
  ],
  active_investigations: [
    { incident_id: 'INC12346', status: 'running' },
  ],
  watch_items: ['Monitor payment-service CPU', 'Check order-db connection pool'],
  upcoming_risk: [
    {
      service: 'payment-service',
      change_type: 'deployment',
      scheduled_at: new Date(Date.now() + 3600000).toISOString(),
      risk_level: 'medium' as const,
      description: 'payment-service v3.2.0 deployment at 02:00 UTC',
    },
  ],
  conditional_guidance: [
    {
      trigger: 'IF payment-service CPU > 90%',
      action: 'THEN scale to 8 replicas immediately',
      escalate_to: 'on-call-lead',
      runbook_hint: 'Scale horizontally first, then investigate root cause',
    },
    {
      trigger: 'IF order-db pool > 95%',
      action: 'THEN restart connection pool with larger limit',
      escalate_to: 'db-team',
      runbook_hint: 'Restart gracefully, not force-kill',
    },
  ],
  open_action_items: [],
}

export const MOCK_POSTMORTEM = {
  report_id: 'pm-001',
  incident_id: 'INC12345',
  service: 'payment-service',
  status: 'draft',
  generated_at: new Date().toISOString(),
  executive_summary: 'A database slow query caused a cascade timeout from payment-service to API gateway affecting 12% of users for 8 minutes.',
  timeline: [
    { timestamp: '2024-02-12T10:30:10Z', event: 'First database latency spike detected (P95 28s)' },
    { timestamp: '2024-02-12T10:30:15Z', event: 'API Gateway timeout alerts fired' },
    { timestamp: '2024-02-12T10:31:00Z', event: 'Root cause identified: missing index on orders table' },
    { timestamp: '2024-02-12T10:32:30Z', event: 'Emergency index added, latency normalised' },
  ],
  five_whys: [
    { why: 'Why did users see timeouts?', answer: 'API Gateway upstream timeout to payment-service exceeded 30s' },
    { why: 'Why was payment-service slow?', answer: 'Database queries hitting orders table with full table scan' },
    { why: 'Why full table scan?', answer: 'Missing index on orders.created_at column' },
    { why: 'Why was index missing?', answer: 'Schema migration CHG-2234 dropped index without recreation' },
    { why: 'Why was this not caught?', answer: 'Migration was not tested against production data volume' },
  ],
  contributing_factors: ['Insufficient staging environment parity', 'No automated index regression check'],
  what_went_well: ['Alert fired within 5 seconds', 'RCA identified in < 2 minutes by AI agent'],
  what_needs_improvement: ['Migration testing process', 'Pre-deployment index validation'],
  action_items: [
    { title: 'Add index validation to CI pipeline', owner: 'Platform Team', due_date: '2024-02-19', priority: 'P1' },
    { title: 'Update staging to match production schema', owner: 'DevOps', due_date: '2024-02-26', priority: 'P2' },
  ],
  prevention_recommendations: ['Automated schema diffing in CI', 'Load test with production-scale data'],
  confidence: 87,
  root_cause: 'Missing index on orders.created_at caused full table scan cascade',
}

export async function mockAllApis(page: Page) {
  // Auth — matches /api/v1/auth/token?role=admin&actor_id=dev-user
  await page.route('**/api/v1/auth/token**', (route) =>
    route.fulfill({ json: { token: 'dev-token-123', actor_id: 'dev-user', role: 'admin' } })
  )

  // Investigations list
  await page.route('**/api/v1/investigations**', (route) => {
    if (route.request().method() === 'GET') {
      route.fulfill({ json: { investigations: MOCK_INVESTIGATIONS, total: 2, limit: 50, offset: 0 } })
    } else {
      route.fulfill({ json: { investigation_id: 'inv-new', incident_id: 'INC-NEW', status: 'running', ws_url: '/ws/inv-new', trace_id: 't1' } })
    }
  })

  // Individual investigation
  await page.route('**/api/v1/investigations/inv-001**', (route) =>
    route.fulfill({ json: MOCK_INVESTIGATIONS[0] })
  )

  // Blast radius
  await page.route('**/api/v1/investigations/*/blast-radius**', (route) =>
    route.fulfill({ json: MOCK_BLAST_RADIUS })
  )

  // Intelligence feed
  await page.route('**/api/v1/intelligence/feed**', (route) =>
    route.fulfill({ json: MOCK_INTELLIGENCE_FEED })
  )

  // Alert acknowledge
  await page.route('**/api/v1/intelligence/alerts/*/acknowledge**', (route) =>
    route.fulfill({ json: { ok: true } })
  )

  // Handoff — current brief (GET) and generate (POST)
  await page.route('**/api/v1/handoff**', (route) =>
    route.fulfill({ json: MOCK_HANDOFF })
  )

  // Postmortem
  await page.route('**/api/v1/postmortem**', (route) =>
    route.fulfill({ json: MOCK_POSTMORTEM })
  )
  await page.route('**/api/v1/postmortem/*/approve**', (route) =>
    route.fulfill({ json: { ...MOCK_POSTMORTEM, status: 'approved' } })
  )
  await page.route('**/api/v1/postmortem/*/publish**', (route) =>
    route.fulfill({ json: { ...MOCK_POSTMORTEM, status: 'published', confluence_url: 'https://wiki.example.com/pm-001' } })
  )

  // Receipts
  await page.route('**/api/v1/investigations/*/receipts**', (route) =>
    route.fulfill({ json: { receipts: [], total: 0, investigation_id: 'inv-001' } })
  )

  // Memory trace
  await page.route('**/api/v1/investigations/*/memory**', (route) =>
    route.fulfill({ json: { investigation_id: 'inv-001', incident_id: 'INC12345', affected_service: 'payment-service', matches: [], total: 0, filters: { min_score: 0.6 } } })
  )

  // Execution graph
  await page.route('**/api/v1/investigations/*/graph**', (route) =>
    route.fulfill({ json: { investigation_id: 'inv-001', incident_id: 'INC12345', trace_id: 't1', schema_version: '1.0', nodes: [], edges: [], created_at: new Date().toISOString(), last_updated_at: new Date().toISOString(), is_complete: true, total_events: 0, event_gaps: [] } })
  )

  // Risk/confidence
  await page.route('**/api/v1/investigations/*/risk**', (route) =>
    route.fulfill({ json: { investigation_id: 'inv-001', confidence: 87, risk_level: 'medium', stale_sources: [], budget_used: 8, budget_max: 20, budget_pct: 40, judge_scores: {}, data_freshness: {}, is_stale: false } })
  )

  // Dev inject (synthetic)
  await page.route('**/api/v1/dev/inject**', (route) =>
    route.fulfill({ json: { investigation_id: 'inv-001', incident_id: 'INC12345', status: 'running', ws_url: '/ws/inv-001', trace_id: 't1' } })
  )

  // WebSocket — do nothing (just let it fail gracefully)
  await page.route('**/ws/**', (route) => route.abort())
}
