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
import { InvestigationSummary } from '@/components/InvestigationSummary'
import MTTRDashboard from '@/components/MTTRDashboard'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { MissionControl } from '@/components/MissionControl'
import { OperationalHealth } from '@/components/OperationalHealth'
import { CausalGraph } from '@/components/CausalGraph'
import { CommandPalette } from '@/components/CommandPalette'
import { useInvestigationStore } from '@/store/investigationStore'
import { recordOperatorEvent, type OperatorMilestone } from '@/api/operatorTelemetry'

// Map an investigation panel to the operator milestone its viewing represents.
const PANEL_MILESTONE: Partial<Record<string, OperatorMilestone>> = {
  timeline: 'timeline_opened',
  graph: 'graph_opened',
  evidence: 'evidence_panel_opened',
}

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
      // Operator timeline: the investigation was opened (real interaction time).
      recordOperatorEvent(investigationId, 'investigation_opened')
    }
    return () => disconnectWS()
  }, [investigationId, connectToInvestigation, disconnectWS])

  // Operator timeline: record which panel the operator viewed.
  useEffect(() => {
    const milestone = PANEL_MILESTONE[activePanel]
    if (investigationId && milestone) {
      recordOperatorEvent(investigationId, milestone, { screen: activePanel })
    }
  }, [investigationId, activePanel])

  if (!investigationId) return null

  return (
    // Progressive enhancement (H-4): stacked on laptop; on ultra-wide (2xl)
    // the "understanding" column (Summary + risk/confidence) sits BESIDE the
    // active panel so current understanding stays visible while drilling in.
    // Below 2xl the 2xl:* classes are inert — laptop layout is unchanged.
    <div className="flex flex-col h-full 2xl:flex-row">
      {/* Understanding column: full-width top strip on laptop, left rail on 2xl */}
      <div className="flex flex-col shrink-0 2xl:w-[22rem] 2xl:h-full 2xl:overflow-y-auto 2xl:border-r 2xl:border-slate-800">
        {/* Persistent Investigation Summary (Phase 2) — answers the 5 operator
            questions without opening a panel, from existing fields only. */}
        <InvestigationSummary />
        {/* Always-visible risk/confidence bar */}
        <RiskConfidenceLayer />
      </div>

      {/* Main content area — the tabpanel controlled by the Sidebar tablist (H-3) */}
      <div className="flex flex-1 overflow-hidden">
        <div
          className="flex-1 overflow-hidden"
          role="tabpanel"
          id="inv-tabpanel"
          aria-labelledby={`inv-tab-${activePanel}`}
          tabIndex={0}
        >
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
