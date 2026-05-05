// AG UI REST API client

import axios from 'axios'
import type {
  IncidentState,
  ExecutionGraph,
  UIReceipt,
  MemoryTraceResponse,
  MemoryScoringResponse,
  RiskConfidence,
  StartInvestigationResponse,
  ControlActionResponse,
  ReplaySession,
  ListInvestigationsResponse,
  ListReceiptsResponse,
} from '@/types'

const BFF_URL = import.meta.env.VITE_BFF_URL || ''

const api = axios.create({
  baseURL: BFF_URL,
  timeout: 30_000,
  headers: { 'Content-Type': 'application/json' },
})

// Attach JWT token to every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('agui_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Expose trace ID from response headers
api.interceptors.response.use(
  (response) => {
    const traceId = response.headers['x-trace-id']
    if (traceId) {
      response.data._trace_id = traceId
    }
    return response
  },
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('agui_token')
      window.location.href = '/login'
    }
    return Promise.reject(error)
  }
)

// ── Investigations ────────────────────────────────────────────────────────────

export const investigationsApi = {
  list: async (params?: {
    status?: string
    limit?: number
    offset?: number
  }): Promise<ListInvestigationsResponse> => {
    const res = await api.get('/api/v1/investigations', { params })
    return res.data
  },

  get: async (investigationId: string): Promise<IncidentState> => {
    const res = await api.get(`/api/v1/investigations/${investigationId}`)
    return res.data
  },

  start: async (incidentId: string, priority = 'normal'): Promise<StartInvestigationResponse> => {
    const res = await api.post('/api/v1/investigations', {
      incident_id: incidentId,
      priority,
    })
    return res.data
  },

  getGraph: async (investigationId: string): Promise<ExecutionGraph> => {
    const res = await api.get(`/api/v1/investigations/${investigationId}/graph`)
    return res.data
  },

  getRisk: async (investigationId: string): Promise<RiskConfidence> => {
    const res = await api.get(`/api/v1/investigations/${investigationId}/risk`)
    return res.data
  },
}

// ── Incidents ─────────────────────────────────────────────────────────────────

export const incidentsApi = {
  list: async (params?: {
    severity?: string
    service?: string
    incident_type?: string
    status?: string
    limit?: number
    offset?: number
  }) => {
    const res = await api.get('/api/v1/incidents', { params })
    return res.data
  },

  get: async (incidentId: string) => {
    const res = await api.get(`/api/v1/incidents/${incidentId}`)
    return res.data
  },
}

// ── Receipts ──────────────────────────────────────────────────────────────────

export const receiptsApi = {
  list: async (investigationId: string): Promise<ListReceiptsResponse> => {
    const res = await api.get(`/api/v1/investigations/${investigationId}/receipts`)
    return res.data
  },

  get: async (investigationId: string, receiptId: string): Promise<UIReceipt> => {
    const res = await api.get(
      `/api/v1/investigations/${investigationId}/receipts/${receiptId}`
    )
    return res.data
  },
}

// ── Memory ────────────────────────────────────────────────────────────────────

export const memoryApi = {
  getTrace: async (
    investigationId: string,
    params?: { min_score?: number; source?: string; service?: string; limit?: number }
  ): Promise<MemoryTraceResponse> => {
    const res = await api.get(
      `/api/v1/investigations/${investigationId}/memory`,
      { params }
    )
    return res.data
  },

  getScoring: async (investigationId: string): Promise<MemoryScoringResponse> => {
    const res = await api.get(
      `/api/v1/investigations/${investigationId}/memory/scoring`
    )
    return res.data
  },
}

// ── Replay ────────────────────────────────────────────────────────────────────

export const replayApi = {
  start: async (
    investigationId: string,
    mode = 'fast',
    speedMultiplier = 5.0
  ): Promise<ReplaySession & { validation: unknown }> => {
    const res = await api.post(
      `/api/v1/investigations/${investigationId}/replay`,
      { mode, speed_multiplier: speedMultiplier }
    )
    return res.data
  },

  getSession: async (investigationId: string, sessionId: string): Promise<ReplaySession> => {
    const res = await api.get(
      `/api/v1/investigations/${investigationId}/replay/${sessionId}`
    )
    return res.data
  },

  control: async (
    investigationId: string,
    sessionId: string,
    action: 'pause' | 'resume' | 'step' | 'abort'
  ) => {
    const res = await api.post(
      `/api/v1/investigations/${investigationId}/replay/${sessionId}/control`,
      { action }
    )
    return res.data
  },
}

// ── Control ───────────────────────────────────────────────────────────────────

export const controlApi = {
  send: async (
    investigationId: string,
    action: string,
    reason?: string,
    targetNodeId?: string
  ): Promise<ControlActionResponse> => {
    const res = await api.post(
      `/api/v1/investigations/${investigationId}/control`,
      { action, reason, target_node_id: targetNodeId }
    )
    return res.data
  },

  list: async (investigationId: string) => {
    const res = await api.get(`/api/v1/investigations/${investigationId}/control`)
    return res.data
  },

  getPending: async (investigationId: string) => {
    const res = await api.get(
      `/api/v1/investigations/${investigationId}/control/pending`
    )
    return res.data
  },
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export const authApi = {
  getDevToken: async (role = 'admin', actorId = 'dev-user') => {
    const res = await api.get('/api/v1/auth/token', {
      params: { role, actor_id: actorId },
    })
    return res.data
  },
}

// ── Dev ───────────────────────────────────────────────────────────────────────

export const devApi = {
  injectSynthetic: async (incidentType = 'error_spike') => {
    const res = await api.post('/api/v1/dev/inject', null, {
      params: { incident_type: incidentType },
    })
    return res.data
  },

  getStatus: async () => {
    const res = await api.get('/api/v1/status')
    return res.data
  },
}

export const harnessApi = {
  getStatus: async () => {
    const res = await api.get('/api/v1/harness/status')
    return res.data
  },

  getSelfReport: async () => {
    const res = await api.get('/api/v1/harness/self-report')
    return res.data
  },

  getReflection: async (investigationId: string) => {
    const res = await api.get(`/api/v1/harness/reflection/${investigationId}`)
    return res.data
  },
}

export default api
