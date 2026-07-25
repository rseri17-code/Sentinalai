import api from './client'

// Operator-timeline telemetry. Fire-and-forget: telemetry must NEVER break or
// slow the operator's workflow, so failures are swallowed. Timestamps are the
// real interaction moment (Date.now, epoch ms) — never synthesized server-side.

export type OperatorMilestone =
  | 'investigation_opened'
  | 'investigation_resumed'
  | 'evidence_panel_opened'
  | 'evidence_item_expanded'
  | 'timeline_opened'
  | 'graph_opened'
  | 'graph_node_selected'
  | 'confidence_viewed'
  | 'owner_viewed'
  | 'recommendation_viewed'
  | 'recommendation_accepted'
  | 'recommendation_rejected'
  | 'next_action_started'
  | 'investigation_completed'
  | 'external_tool_opened'

export interface OperatorEventExtra {
  service?: string
  application?: string
  entity?: string
  screen?: string
  duration_ms?: number
  tool_name?: string
  reason?: string
  time_away_ms?: number
}

export function recordOperatorEvent(
  investigationId: string,
  milestone: OperatorMilestone,
  extra: OperatorEventExtra = {}
): void {
  if (!investigationId) return
  const body = { milestone, at: Date.now(), ...extra }
  // deliberately not awaited; errors are non-fatal to the operator workflow
  api
    .post(`/api/v1/investigations/${investigationId}/operator-events`, body)
    .catch(() => undefined)
}
