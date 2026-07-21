import React, { useEffect } from 'react'
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
import { MttiTimeline } from '@/components/MttiTimeline'
import MTTRDashboard from '@/components/MTTRDashboard'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { MissionControl } from '@/components/MissionControl'
import { OperationalHealth } from '@/components/OperationalHealth'
import { CausalGraph } from '@/components/CausalGraph'
import { CommandPalette } from '@/components/CommandPalette'
import { useInvestigationStore } from '@/store/investigationStore'

function KnowledgeGraphPage() {
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0 flex items-center gap-2">
        <span className="text-sm font-medium text-slate-300">Knowledge Graph</span>
        <span className="text-[10px] text-slate-500 ml-1">— Service topology &amp; blast radius</span>
      </div>
      <div className="flex-1 overflow-hidden">
        <CausalGraph />
      </div>
    </div>
  )
}

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
          {activePanel === 'mtti' && <ErrorBoundary label="MTTI"><MttiTimeline /></ErrorBoundary>}
        </div>
      </div>
    </div>
  )
}

export function AppShell() {
  return (
    <div className="flex h-screen bg-slate-950 overflow-hidden">
      <Sidebar />
      <div className="flex flex-col flex-1 overflow-hidden">
        <TopBar />
        <CommandPalette />
        <main className="flex-1 overflow-hidden">
          <Routes>
            <Route path="/investigations" element={<ErrorBoundary label="Mission Control"><MissionControl /></ErrorBoundary>} />
            <Route path="/investigations/:investigationId" element={<ErrorBoundary label="Investigation"><InvestigationView /></ErrorBoundary>} />
            <Route path="/dashboard" element={<ErrorBoundary label="MTTR Dashboard"><MTTRDashboard /></ErrorBoundary>} />
            <Route path="/operational-health" element={<ErrorBoundary label="Operational Health"><OperationalHealth /></ErrorBoundary>} />
            <Route path="/graph" element={<ErrorBoundary label="Knowledge Graph"><KnowledgeGraphPage /></ErrorBoundary>} />
            <Route path="*" element={<Navigate to="/investigations" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  )
}
