// AG UI Zustand Investigation Store
// Single source of truth for all investigation state in the UI

import { create } from 'zustand'
import type {
  AGUIEvent,
  ExecutionGraph,
  UIReceipt,
  IncidentState,
  ControlAction,
  MemoryMatch,
  ReplaySession,
  WSStatus,
  ActivePanel,
  HypothesisSummary,
  HarnessReflection,
} from '@/types'
import { AGUIWebSocket } from '@/api/ws'
import { investigationsApi, receiptsApi, replayApi, controlApi } from '@/api/client'

interface ReplayState {
  session: ReplaySession | null
  isActive: boolean
  currentStep: number
  totalSteps: number
  mode: 'live' | 'fast' | 'step'
}

interface InvestigationStore {
  // Current investigation
  investigation: IncidentState | null
  investigationId: string | null

  // Event stream
  events: AGUIEvent[]
  latestEvent: AGUIEvent | null

  // Graph
  graph: ExecutionGraph | null
  selectedNodeId: string | null

  // Receipts
  receipts: UIReceipt[]
  selectedReceiptId: string | null

  // Memory
  memoryMatches: MemoryMatch[]

  // Control
  controlActions: ControlAction[]
  pendingApproval: string | null

  // Replay
  replayState: ReplayState

  // WebSocket
  ws: AGUIWebSocket | null
  wsStatus: WSStatus
  connectionId: string | null

  // Harness / self-awareness
  harnessReflection: HarnessReflection | null
  harnessPhase: string | null

  // UI state
  activePanel: ActivePanel
  isDrawerOpen: boolean
  drawerReceiptId: string | null

  // Actions
  connectToInvestigation: (investigationId: string, lastSeq?: number) => void
  disconnectWS: () => void
  setActivePanel: (panel: ActivePanel) => void
  selectNode: (nodeId: string | null) => void
  openReceiptDrawer: (receiptId: string) => void
  closeDrawer: () => void

  // Investigation actions
  refreshInvestigation: () => Promise<void>
  refreshGraph: () => Promise<void>
  refreshReceipts: () => Promise<void>

  // Replay actions
  startReplay: (mode?: 'live' | 'fast' | 'step', speed?: number) => Promise<void>
  controlReplay: (action: 'pause' | 'resume' | 'step' | 'abort') => Promise<void>

  // Control actions
  sendControlAction: (
    action: string,
    reason?: string,
    targetNodeId?: string
  ) => Promise<void>

  // Internal
  _handleEvent: (event: AGUIEvent) => void
  _updateGraphFromEvent: (event: AGUIEvent) => void
}

export const useInvestigationStore = create<InvestigationStore>((set, get) => ({
  investigation: null,
  investigationId: null,
  events: [],
  latestEvent: null,
  graph: null,
  selectedNodeId: null,
  receipts: [],
  selectedReceiptId: null,
  memoryMatches: [],
  controlActions: [],
  pendingApproval: null,
  replayState: {
    session: null,
    isActive: false,
    currentStep: 0,
    totalSteps: 0,
    mode: 'fast',
  },
  ws: null,
  wsStatus: 'disconnected',
  connectionId: null,
  harnessReflection: null,
  harnessPhase: null,
  activePanel: 'timeline',
  isDrawerOpen: false,
  drawerReceiptId: null,

  connectToInvestigation: (investigationId, lastSeq = 0) => {
    const existing = get().ws
    if (existing) {
      existing.disconnect()
    }

    set({ investigationId, events: [], graph: null, receipts: [], harnessReflection: null, harnessPhase: null })

    // Load initial state
    investigationsApi.get(investigationId)
      .then((inv) => set({ investigation: inv }))
      .catch(console.error)

    investigationsApi.getGraph(investigationId)
      .then((graph) => set({ graph }))
      .catch(console.error)

    receiptsApi.list(investigationId)
      .then((res) => set({ receipts: res.receipts }))
      .catch(console.error)

    // Connect WebSocket
    const ws = new AGUIWebSocket({
      investigationId,
      lastSeq,
      onEvent: (event) => get()._handleEvent(event),
      onStatus: (status) => set({ wsStatus: status }),
      onConnectionAck: (connectionId) => set({ connectionId }),
    })
    set({ ws })
  },

  disconnectWS: () => {
    get().ws?.disconnect()
    set({ ws: null, wsStatus: 'disconnected' })
  },

  setActivePanel: (panel) => set({ activePanel: panel }),

  selectNode: (nodeId) => {
    set({ selectedNodeId: nodeId })
    if (nodeId) {
      // Find receipt for this node
      const graph = get().graph
      const node = graph?.nodes.find((n) => n.node_id === nodeId)
      if (node?.receipt_id) {
        set({ selectedReceiptId: node.receipt_id })
      }
    }
  },

  openReceiptDrawer: (receiptId) =>
    set({ isDrawerOpen: true, drawerReceiptId: receiptId }),

  closeDrawer: () =>
    set({ isDrawerOpen: false, drawerReceiptId: null }),

  refreshInvestigation: async () => {
    const id = get().investigationId
    if (!id) return
    const inv = await investigationsApi.get(id)
    set({
      investigation: inv,
      memoryMatches: inv.memory_matches,
      controlActions: inv.control_actions,
    })
  },

  refreshGraph: async () => {
    const id = get().investigationId
    if (!id) return
    const graph = await investigationsApi.getGraph(id)
    set({ graph })
  },

  refreshReceipts: async () => {
    const id = get().investigationId
    if (!id) return
    const res = await receiptsApi.list(id)
    set({ receipts: res.receipts })
  },

  startReplay: async (mode = 'fast', speed = 5.0) => {
    const id = get().investigationId
    if (!id) return
    const session = await replayApi.start(id, mode, speed)
    set({
      replayState: {
        session,
        isActive: true,
        currentStep: 0,
        totalSteps: session.total_steps,
        mode,
      },
      activePanel: 'timeline',
    })
  },

  controlReplay: async (action) => {
    const id = get().investigationId
    const session = get().replayState.session
    if (!id || !session) return
    await replayApi.control(id, session.session_id, action)
    if (action === 'abort') {
      set((s) => ({
        replayState: { ...s.replayState, isActive: false },
      }))
    }
  },

  sendControlAction: async (action, reason, targetNodeId) => {
    const id = get().investigationId
    const inv = get().investigation
    if (!id || !inv) return
    const result = await controlApi.send(id, action, reason, targetNodeId)
    // Optimistically add to control actions
    const newAction: ControlAction = {
      action_id: result.action_id,
      investigation_id: id,
      incident_id: inv.incident_id,
      action_type: action as ControlAction['action_type'],
      actor_id: result.actor_id,
      actor_role: 'operator',
      reason,
      target_node_id: targetNodeId,
      timestamp: result.timestamp,
      status: 'applied',
      metadata: {},
    }
    set((s) => ({
      controlActions: [...s.controlActions, newAction],
      pendingApproval: null,
    }))
  },

  _handleEvent: (event) => {
    set((state) => ({
      events: [...state.events, event],
      latestEvent: event,
    }))

    const { event_type, payload } = event

    // Update investigation state from events
    if (event_type === 'investigation.completed') {
      set((s) => ({
        investigation: s.investigation
          ? {
              ...s.investigation,
              status: 'completed',
              confidence: (payload.confidence as number) ?? s.investigation.confidence,
            }
          : s.investigation,
      }))
    }

    if (event_type === 'investigation.failed') {
      set((s) => ({
        investigation: s.investigation
          ? { ...s.investigation, status: 'failed' }
          : s.investigation,
      }))
    }

    if (event_type === 'investigation.paused') {
      set((s) => ({
        investigation: s.investigation
          ? { ...s.investigation, status: 'paused' }
          : s.investigation,
        pendingApproval: (payload.awaiting_approval_for as string) || null,
      }))
    }

    if (event_type === 'hypothesis.scored') {
      const hyps = (payload.hypotheses as Array<{ name: string; score: number }>) || []
      const winner = payload.winner as string
      const confidence = payload.confidence as number
      const hypotheses: HypothesisSummary[] = hyps.map((h) => ({
        name: h.name,
        root_cause: '',
        score: h.score,
        is_winner: h.name === winner,
        evidence_refs: [],
      }))
      set((s) => ({
        investigation: s.investigation
          ? { ...s.investigation, hypotheses, confidence, winner_hypothesis: winner }
          : s.investigation,
      }))
    }

    if (event_type === 'rca.generated') {
      set((s) => ({
        investigation: s.investigation
          ? {
              ...s.investigation,
              root_cause: (payload.root_cause as string) || s.investigation.root_cause,
              confidence: (payload.confidence as number) ?? s.investigation.confidence,
            }
          : s.investigation,
      }))
    }

    if (event_type === 'control.requested') {
      set({ pendingApproval: payload.reason as string || 'Approval required' })
    }

    if (event_type === 'control.approved' || event_type === 'control.rejected') {
      set({ pendingApproval: null })
    }

    // Update replay progress
    if ((payload as Record<string, unknown>)._replay) {
      const step = payload._replay_step as number
      const total = payload._replay_total as number
      set((s) => ({
        replayState: { ...s.replayState, currentStep: step, totalSteps: total },
      }))
    }

    // Refresh graph after events that change graph structure
    const graphEvents = [
      'tool.responded', 'tool.failed', 'hypothesis.scored',
      'hypothesis.selected', 'rca.generated', 'investigation.completed',
      'control.requested', 'control.approved',
    ]
    if (graphEvents.includes(event_type)) {
      // Debounce graph refresh
      const id = get().investigationId
      if (id) {
        investigationsApi.getGraph(id)
          .then((graph) => set({ graph }))
          .catch(() => null)
      }
    }

    // Harness reflection events — live self-awareness feed
    if (event_type === 'harness.reflection' || event_type === 'harness.complete') {
      const phase = payload?.phase as string | undefined
      set({ harnessPhase: phase || 'complete' })
      if (phase === 'reflection_complete' || event_type === 'harness.complete') {
        set({ harnessReflection: payload as unknown as HarnessReflection })
      }
    }
    if (event_type === 'harness.pre_flight') {
      set({ harnessPhase: 'pre_flight' })
    }
    if (event_type === 'harness.correction') {
      set({ harnessPhase: 'correcting' })
    }
  },

  _updateGraphFromEvent: (_event) => {
    // Graph is refreshed from server after key events
  },
}))

// ── Incident list store ───────────────────────────────────────────────────────

interface IncidentListStore {
  incidents: IncidentState[]
  total: number
  loading: boolean
  filters: {
    severity?: string
    service?: string
    incident_type?: string
    status?: string
  }
  pagination: { limit: number; offset: number }

  load: () => Promise<void>
  setFilter: (key: string, value: string | undefined) => void
  setPage: (offset: number) => void
}

import { incidentsApi } from '@/api/client'

export const useIncidentListStore = create<IncidentListStore>((set, get) => ({
  incidents: [],
  total: 0,
  loading: false,
  filters: {},
  pagination: { limit: 50, offset: 0 },

  load: async () => {
    set({ loading: true })
    try {
      const { filters, pagination } = get()
      const res = await incidentsApi.list({ ...filters, ...pagination })
      set({ incidents: res.incidents, total: res.total })
    } finally {
      set({ loading: false })
    }
  },

  setFilter: (key, value) => {
    set((s) => ({
      filters: { ...s.filters, [key]: value },
      pagination: { ...s.pagination, offset: 0 },
    }))
    get().load()
  },

  setPage: (offset) => {
    set((s) => ({ pagination: { ...s.pagination, offset } }))
    get().load()
  },
}))

// ── User/Auth store ───────────────────────────────────────────────────────────

import type { AppUser, UserRole } from '@/types'

interface AuthStore {
  user: AppUser | null
  setUser: (user: AppUser | null) => void
  hasRole: (minimum: UserRole) => boolean
}

const ROLE_LEVELS: Record<UserRole, number> = {
  viewer: 0, operator: 1, approver: 2, admin: 3,
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  user: null,
  setUser: (user) => set({ user }),
  hasRole: (minimum) => {
    const user = get().user
    if (!user) return false
    return (ROLE_LEVELS[user.role] ?? -1) >= (ROLE_LEVELS[minimum] ?? 99)
  },
}))
