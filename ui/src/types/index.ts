// AG UI TypeScript Type Definitions
// Single source of truth — mirrors Python agui/schemas/

// ── Event Types ──────────────────────────────────────────────────────────────

export type EventType =
  | 'investigation.started'
  | 'investigation.completed'
  | 'investigation.failed'
  | 'investigation.paused'
  | 'investigation.resumed'
  | 'tool.called'
  | 'tool.responded'
  | 'tool.failed'
  | 'tool.timeout'
  | 'hypothesis.generated'
  | 'hypothesis.scored'
  | 'hypothesis.selected'
  | 'llm.invoked'
  | 'llm.responded'
  | 'llm.failed'
  | 'memory.queried'
  | 'memory.result'
  | 'memory.stored'
  | 'control.requested'
  | 'control.approved'
  | 'control.rejected'
  | 'control.timeout'
  | 'budget.warning'
  | 'budget.exhausted'
  | 'circuit_breaker.tripped'
  | 'circuit_breaker.reset'
  | 'incident.classified'
  | 'playbook.selected'
  | 'rca.generated'
  | 'replay.started'
  | 'replay.step'
  | 'replay.completed'
  | 'system.heartbeat'
  | 'connection.ack'
  | 'subscribed'
  | 'pong'
  | 'heartbeat'

export interface AGUIEvent {
  event_id: string
  schema_version: string
  idempotency_key: string
  event_type: EventType
  investigation_id: string
  incident_id: string
  trace_id: string
  span_id: string
  parent_span_id?: string
  sequence_num: number
  timestamp: string
  timestamp_epoch_ms: number
  payload: Record<string, unknown>
  ttl: number
  // Replay metadata (injected by replay engine)
  _replay?: boolean
  _replay_step?: number
  _replay_total?: number
}

// ── Graph Types ───────────────────────────────────────────────────────────────

export type NodeType =
  | 'investigation'
  | 'classification'
  | 'playbook'
  | 'tool_call'
  | 'llm_inference'
  | 'hypothesis'
  | 'memory_query'
  | 'decision'
  | 'control'
  | 'rca'

export type NodeStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'timeout'
  | 'awaiting_approval'
  | 'approved'
  | 'rejected'
  | 'skipped'

export interface GraphNode {
  node_id: string
  schema_version: string
  node_type: NodeType
  label: string
  investigation_id: string
  trace_id: string
  span_id: string
  parent_ids: string[]
  child_ids: string[]
  status: NodeStatus
  started_at?: string
  completed_at?: string
  duration_ms?: number
  receipt_id?: string
  event_id?: string
  sequence_num: number
  worker?: string
  action?: string
  tool?: string
  confidence?: number
  hypothesis_name?: string
  llm_model?: string
  token_count?: number
  metadata: Record<string, unknown>
  error_message?: string
  x?: number
  y?: number
}

export interface GraphEdge {
  edge_id: string
  source_id: string
  target_id: string
  edge_type: string
  label?: string
  metadata: Record<string, unknown>
}

export interface ExecutionGraph {
  investigation_id: string
  incident_id: string
  trace_id: string
  schema_version: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  created_at: string
  last_updated_at: string
  is_complete: boolean
  total_events: number
  event_gaps: number[]
}

// ── Receipt Types ─────────────────────────────────────────────────────────────

export interface UIReceipt {
  receipt_id: string
  schema_version: string
  investigation_id: string
  incident_id: string
  sequence_num: number
  trace_id: string
  span_id: string
  correlation_id: string
  worker: string
  tool: string
  action: string
  mcp_target: string
  wall_clock_start: string
  wall_clock_end: string
  elapsed_ms: number
  status: 'pending' | 'success' | 'error' | 'timeout'
  error?: string
  result_count: number
  params_redacted: Record<string, unknown>
  output_summary?: string
  evidence_refs: string[]
  storage_uri?: string
  payload_hash: string
}

// ── Incident State Types ──────────────────────────────────────────────────────

export type InvestigationStatus =
  | 'pending'
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'paused'
  | 'replaying'

export type IncidentSeverity = 'critical' | 'major' | 'warning' | 'minor' | 'info'
export type RiskLevel = 'low' | 'medium' | 'high' | 'critical' | 'unknown'

export interface HypothesisSummary {
  name: string
  root_cause: string
  score: number
  is_winner: boolean
  evidence_refs: string[]
}

export interface MemoryMatch {
  incident_id: string
  summary: string
  service: string
  similarity_score: number
  root_cause?: string
  resolution?: string
  occurred_at?: string
  source: 'stm' | 'ltm' | 'knowledge_graph'
}

export type ControlActionType =
  | 'approve'
  | 'reject'
  | 'pause'
  | 'resume'
  | 'override'
  | 'escalate'

export interface ControlAction {
  action_id: string
  investigation_id: string
  incident_id: string
  action_type: ControlActionType
  actor_id: string
  actor_role: string
  reason?: string
  target_node_id?: string
  timestamp: string
  status: string
  applied_at?: string
  metadata: Record<string, unknown>
}

export interface IncidentState {
  investigation_id: string
  incident_id: string
  schema_version: string
  trace_id: string
  x_ray_trace_url?: string
  summary: string
  affected_service: string
  severity: string
  incident_type: string
  source: string
  status: InvestigationStatus
  playbook: string[]
  started_at?: string
  completed_at?: string
  duration_ms?: number
  root_cause?: string
  confidence: number
  risk_level: string
  hypotheses: HypothesisSummary[]
  winner_hypothesis?: string
  receipt_ids: string[]
  tool_calls_total: number
  tool_calls_success: number
  tool_calls_failed: number
  memory_matches: MemoryMatch[]
  judge_scores: Record<string, number>
  control_actions: ControlAction[]
  awaiting_approval_for?: string
  data_freshness: Record<string, string>
  stale_sources: string[]
  replay_available: boolean
  replay_artifact_uri?: string
  event_count: number
  budget_used: number
  budget_max: number
}

// ── Risk + Confidence ─────────────────────────────────────────────────────────

export interface RiskConfidence {
  investigation_id: string
  confidence: number
  risk_level: string
  stale_sources: string[]
  budget_used: number
  budget_max: number
  budget_pct: number
  judge_scores: Record<string, number>
  data_freshness: Record<string, string>
  is_stale: boolean
}

// ── Replay Types ──────────────────────────────────────────────────────────────

export type ReplayMode = 'live' | 'fast' | 'step'
export type ReplayStatus = 'pending' | 'running' | 'paused' | 'completed' | 'failed' | 'aborted'

export interface ReplaySession {
  session_id: string
  investigation_id: string
  status: ReplayStatus
  mode: ReplayMode
  current_step: number
  total_steps: number
  progress_pct: number
  actor_id: string
  started_at?: number
  completed_at?: number
}

// ── UI State Types ────────────────────────────────────────────────────────────

export type WSStatus = 'connecting' | 'connected' | 'disconnected' | 'error'
export type ActivePanel = 'timeline' | 'graph' | 'evidence' | 'memory' | 'replay' | 'control'
export type UserRole = 'viewer' | 'operator' | 'approver' | 'admin'

export interface AppUser {
  actor_id: string
  role: UserRole
  email?: string
  token?: string
}

// ── API Response Types ────────────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface ListInvestigationsResponse {
  investigations: IncidentState[]
  total: number
  limit: number
  offset: number
}

export interface ListReceiptsResponse {
  receipts: UIReceipt[]
  total: number
  investigation_id: string
}

export interface MemoryTraceResponse {
  investigation_id: string
  incident_id: string
  affected_service: string
  matches: MemoryMatch[]
  total: number
  filters: {
    min_score: number
    source?: string
    service?: string
  }
}

export interface MemoryScoringResponse {
  investigation_id: string
  scoring: {
    confidence: number
    risk_level: string
    judge_scores: Record<string, number>
    judge_average: number
    evidence_completeness: number
    hypothesis_count: number
    winner_hypothesis: string
  }
  budget: {
    used: number
    max: number
    pct: number
    tool_calls_success: number
    tool_calls_failed: number
    tool_calls_total: number
  }
  hypotheses: HypothesisSummary[]
  data_freshness: Record<string, string>
  stale_sources: string[]
}

export interface StartInvestigationResponse {
  investigation_id: string
  incident_id: string
  status: string
  ws_url: string
  trace_id: string
}

export interface ControlActionResponse {
  action_id: string
  investigation_id: string
  action: string
  status: string
  actor_id: string
  timestamp: string
}
