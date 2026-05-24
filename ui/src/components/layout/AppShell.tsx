import React, { useEffect, useMemo, useState } from 'react'
import { Routes, Route, Navigate, useParams } from 'react-router-dom'
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
import { ArchitectureMiniMap } from '@/components/ArchitectureMiniMap'
import { PatternIntelligencePanel } from '@/components/PatternIntelligencePanel'
import { MissionControl } from '@/components/MissionControl'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { useInvestigationStore } from '@/store/investigationStore'
import { computeMTTI, fmtDuration, elapsedSince } from '@/utils/mtti'
import { Clock, Brain } from 'lucide-react'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// MTTI badge — left context pane
// ---------------------------------------------------------------------------

function MTTIBadge() {
  const { investigation, events } = useInvestigationStore()
  const [tick, setTick] = useState(0)

  // Re-render every second while investigation is running
  useEffect(() => {
    if (investigation?.status !== 'running') return
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [investigation?.status])

  const mtti = useMemo(() => computeMTTI(events, investigation), [events, investigation, tick])
  const isRunning = investigation?.status === 'running'
  const elapsed = investigation?.started_at ? elapsedSince(investigation.started_at) : null

  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 p-2.5 space-y-2">
      {/* MTTI */}
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-slate-500 uppercase tracking-wider font-medium flex items-center gap-1">
          <Clock size={9} />
          MTTI
        </span>
        {mtti !== null ? (
          <span className="text-green-400 font-mono font-semibold text-sm">{fmtDuration(mtti)}</span>
        ) : isRunning ? (
          <span className="text-blue-400 font-mono text-sm animate-pulse">
            {elapsed ? fmtDuration(elapsed) : '—'}
          </span>
        ) : (
          <span className="text-slate-600 text-xs">—</span>
        )}
      </div>
      <div className="text-[9px] text-slate-600">
        {mtti !== null
          ? 'Root cause identified ✓'
          : isRunning
          ? 'Investigating…'
          : 'Not started'}
      </div>

      {/* Confidence */}
      {investigation && investigation.confidence > 0 && (
        <div className="flex items-center justify-between border-t border-slate-800 pt-2">
          <span className="text-[10px] text-slate-500 flex items-center gap-1">
            <Brain size={9} />
            Confidence
          </span>
          <span className={clsx(
            'font-mono text-sm font-semibold',
            investigation.confidence >= 0.8 ? 'text-green-400' :
            investigation.confidence >= 0.6 ? 'text-yellow-400' : 'text-red-400'
          )}>
            {(investigation.confidence * 100).toFixed(0)}%
          </span>
        </div>
      )}

      {/* Budget */}
      {investigation && (
        <div className="border-t border-slate-800 pt-2 space-y-1">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-slate-500">Budget</span>
            <span className="text-slate-400 font-mono">
              {investigation.budget_used}/{investigation.budget_max}
            </span>
          </div>
          <div className="h-1 bg-slate-800 rounded-full overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full transition-all',
                (investigation.budget_used / investigation.budget_max) > 0.8
                  ? 'bg-red-500'
                  : 'bg-blue-500'
              )}
              style={{
                width: `${Math.min(100, (investigation.budget_used / investigation.budget_max) * 100)}%`
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 3-column layout for the Timeline panel
// ---------------------------------------------------------------------------

function ThreeColumnLayout() {
  return (
    <div className="flex h-full overflow-hidden">
      {/* Left: context pane — arch mini-map + MTTI + risk */}
      <div className="w-64 shrink-0 border-r border-slate-800 flex flex-col gap-3 p-3 overflow-y-auto bg-slate-950/40">
        <MTTIBadge />
        <ArchitectureMiniMap />
        <RiskConfidenceLayer compact />
      </div>

      {/* Center: investigation story */}
      <div className="flex-1 overflow-hidden">
        <ErrorBoundary label="Timeline">
          <IncidentCommandCenter />
        </ErrorBoundary>
      </div>

      {/* Right: pattern intelligence */}
      <div className="w-64 shrink-0 border-l border-slate-800 flex flex-col overflow-hidden bg-slate-950/40">
        <div className="px-3 py-2 border-b border-slate-800 shrink-0">
          <span className="text-[10px] text-slate-500 uppercase tracking-wider font-medium">
            Pattern Intelligence
          </span>
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <PatternIntelligencePanel />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Investigation view
// ---------------------------------------------------------------------------

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

  // Timeline panel: use 3-column persistent layout.
  // Deep-dive panels (graph, replay, etc.): full-width with risk bar.
  if (activePanel === 'timeline') {
    return (
      <div className="flex flex-col h-full">
        <TopBarInvestigation />
        <div className="flex-1 overflow-hidden">
          <ThreeColumnLayout />
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <TopBarInvestigation />
      <RiskConfidenceLayer />
      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-hidden">
          {activePanel === 'graph'      && <ErrorBoundary label="Execution Graph"><ExecutionGraph /></ErrorBoundary>}
          {activePanel === 'evidence'   && <ErrorBoundary label="Evidence"><EvidenceDrawer /></ErrorBoundary>}
          {activePanel === 'memory'     && <ErrorBoundary label="Memory Trace"><MemoryTracePanel /></ErrorBoundary>}
          {activePanel === 'replay'     && <ErrorBoundary label="Replay"><ReplayMode /></ErrorBoundary>}
          {activePanel === 'control'    && <ErrorBoundary label="Control Panel"><ControlPanel /></ErrorBoundary>}
          {activePanel === 'reflection' && <ErrorBoundary label="Self-Awareness"><ReflectionPanel /></ErrorBoundary>}
          {activePanel === 'tools'      && <ErrorBoundary label="Tool Inspector"><ToolCallInspector /></ErrorBoundary>}
        </div>
      </div>
    </div>
  )
}

// Inline TopBar for investigation context (avoids re-rendering TopBar which reads store)
function TopBarInvestigation() {
  return <TopBar />
}

export function AppShell() {
  return (
    <div className="flex h-screen bg-slate-950 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        {/* TopBar shown only for non-investigation routes (investigation view has its own) */}
        <Routes>
          <Route path="/investigations/:investigationId/*" element={null} />
          <Route path="*" element={<TopBar />} />
        </Routes>
        <main className="flex-1 overflow-hidden">
          <Routes>
            <Route path="/investigations" element={<ErrorBoundary label="Mission Control"><MissionControl /></ErrorBoundary>} />
            <Route path="/investigations/:investigationId" element={<ErrorBoundary label="Investigation"><InvestigationView /></ErrorBoundary>} />
            <Route path="/dashboard" element={<ErrorBoundary label="MTTI · MTTR Dashboard"><MTTRDashboard /></ErrorBoundary>} />
            <Route path="/architecture" element={<ErrorBoundary label="Neural Architecture"><NeuralArchitecturePanel /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/investigations" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
