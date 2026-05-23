import React, { useEffect } from 'react'
import { Routes, Route, Navigate, useParams, useNavigate } from 'react-router-dom'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { IncidentCommandCenter } from '@/components/IncidentCommandCenter'
import { ExecutionGraph } from '@/components/ExecutionGraph'
import { EvidenceDrawer } from '@/components/EvidenceDrawer'
import { MemoryTracePanel } from '@/components/MemoryTracePanel'
import { ReplayMode } from '@/components/ReplayMode'
import { ControlPanel } from '@/components/ControlPanel'
import { RiskConfidenceLayer } from '@/components/RiskConfidenceLayer'
import { ReflectionPanel } from '@/components/ReflectionPanel'
import { ToolCallInspector } from '@/components/ToolCallInspector'
import MTTRDashboard from '@/components/MTTRDashboard'
import { NeuralArchitecturePanel } from '@/components/NeuralArchitecturePanel'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { useInvestigationStore } from '@/store/investigationStore'

function InvestigationView() {
  const { investigationId } = useParams<{ investigationId: string }>()
  const { connectToInvestigation, disconnectWS, activePanel } = useInvestigationStore()

  useEffect(() => {
    if (investigationId) {
      connectToInvestigation(investigationId)
    }
    return () => disconnectWS()
  }, [investigationId, connectToInvestigation, disconnectWS])

  if (!investigationId) return null

  return (
    <div className="flex flex-col h-full">
      {/* Always-visible risk/confidence bar */}
      <RiskConfidenceLayer />

      {/* Main content area */}
      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-hidden">
          {activePanel === 'timeline' && <ErrorBoundary label="Timeline"><IncidentCommandCenter /></ErrorBoundary>}
          {activePanel === 'graph' && <ErrorBoundary label="Execution Graph"><ExecutionGraph /></ErrorBoundary>}
          {activePanel === 'evidence' && <ErrorBoundary label="Evidence"><EvidenceDrawer /></ErrorBoundary>}
          {activePanel === 'memory' && <ErrorBoundary label="Memory Trace"><MemoryTracePanel /></ErrorBoundary>}
          {activePanel === 'replay' && <ErrorBoundary label="Replay"><ReplayMode /></ErrorBoundary>}
          {activePanel === 'control' && <ErrorBoundary label="Control Panel"><ControlPanel /></ErrorBoundary>}
          {activePanel === 'reflection' && <ErrorBoundary label="Self-Awareness"><ReflectionPanel /></ErrorBoundary>}
          {activePanel === 'tools' && <ErrorBoundary label="Tool Inspector"><ToolCallInspector /></ErrorBoundary>}
        </div>
      </div>
    </div>
  )
}

function InvestigationsList() {
  const navigate = useNavigate()
  const handleInject = async (type: string) => {
    try {
      const { devApi } = await import('@/api/client')
      const res = await devApi.injectSynthetic(type)
      navigate(`/investigations/${res.investigation_id}`)
    } catch (err) {
      console.error(err)
    }
  }

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-100">Investigations</h1>
        <div className="flex gap-2">
          {['error_spike', 'oomkill', 'latency', 'timeout'].map((t) => (
            <button
              key={t}
              onClick={() => handleInject(t)}
              className="btn-primary text-xs"
            >
              + {t}
            </button>
          ))}
        </div>
      </div>
      <InvestigationTable />
    </div>
  )
}

function InvestigationTable() {
  const navigate = useNavigate()
  const [investigations, setInvestigations] = React.useState<import('@/types').IncidentState[]>([])

  useEffect(() => {
    import('@/api/client').then(({ investigationsApi }) => {
      investigationsApi.list({ limit: 50 }).then((res) => setInvestigations(res.investigations))
    })
  }, [])

  if (investigations.length === 0) {
    return (
      <div className="panel p-8 text-center text-slate-500">
        No investigations yet. Click a button above to inject a synthetic incident.
      </div>
    )
  }

  return (
    <div className="panel overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-slate-400 text-left">
            <th className="px-4 py-3">Incident ID</th>
            <th className="px-4 py-3">Service</th>
            <th className="px-4 py-3">Type</th>
            <th className="px-4 py-3">Severity</th>
            <th className="px-4 py-3">Status</th>
            <th className="px-4 py-3">Confidence</th>
            <th className="px-4 py-3">Started</th>
          </tr>
        </thead>
        <tbody>
          {investigations.map((inv) => (
            <tr
              key={inv.investigation_id}
              className="border-b border-slate-800/50 hover:bg-slate-800/50 cursor-pointer"
              onClick={() => navigate(`/investigations/${inv.investigation_id}`)}
            >
              <td className="px-4 py-3 font-mono text-blue-400">{inv.incident_id}</td>
              <td className="px-4 py-3">{inv.affected_service || '—'}</td>
              <td className="px-4 py-3 text-slate-400">{inv.incident_type || '—'}</td>
              <td className="px-4 py-3">
                <SeverityBadge severity={inv.severity} />
              </td>
              <td className="px-4 py-3">
                <StatusBadge status={inv.status} />
              </td>
              <td className="px-4 py-3">
                {inv.confidence > 0 ? `${(inv.confidence * 100).toFixed(0)}%` : '—'}
              </td>
              <td className="px-4 py-3 text-slate-400 text-xs">
                {inv.started_at ? new Date(inv.started_at).toLocaleString() : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function SeverityBadge({ severity }: { severity: string }) {
  const colors: Record<string, string> = {
    critical: 'bg-red-900/50 text-red-400 border border-red-800',
    major: 'bg-orange-900/50 text-orange-400 border border-orange-800',
    warning: 'bg-yellow-900/50 text-yellow-400 border border-yellow-800',
    minor: 'bg-green-900/50 text-green-400 border border-green-800',
    info: 'bg-sky-900/50 text-sky-400 border border-sky-800',
  }
  return (
    <span className={`badge ${colors[severity] || 'bg-slate-800 text-slate-400'}`}>
      {severity}
    </span>
  )
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    running: 'bg-blue-900/50 text-blue-400',
    completed: 'bg-green-900/50 text-green-400',
    failed: 'bg-red-900/50 text-red-400',
    pending: 'bg-slate-800 text-slate-400',
    paused: 'bg-yellow-900/50 text-yellow-400',
    awaiting_approval: 'bg-purple-900/50 text-purple-400',
  }
  return (
    <span className={`badge ${colors[status] || 'bg-slate-800 text-slate-400'}`}>
      {status}
    </span>
  )
}

export function AppShell() {
  return (
    <div className="flex h-screen bg-slate-950 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <TopBar />
        <main className="flex-1 overflow-hidden">
          <Routes>
            <Route path="/investigations" element={<ErrorBoundary label="Investigations"><InvestigationsList /></ErrorBoundary>} />
            <Route path="/investigations/:investigationId" element={<ErrorBoundary label="Investigation"><InvestigationView /></ErrorBoundary>} />
            <Route path="/dashboard" element={<ErrorBoundary label="MTTR Dashboard"><MTTRDashboard /></ErrorBoundary>} />
            <Route path="/architecture" element={<ErrorBoundary label="Dynamic Architecture"><NeuralArchitecturePanel /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/investigations" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
