import React from 'react'
import { NavLink, useParams } from 'react-router-dom'
import {
  LayoutDashboard, GitBranch, FileText, Brain, RotateCcw,
  Shield, AlertTriangle, BarChart2, Sparkles, Wrench, Network, Zap,
} from 'lucide-react'
import { useInvestigationStore, useAuthStore } from '@/store/investigationStore'
import type { ActivePanel } from '@/types'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Deep-dive panels — only shown when inside an investigation
// ---------------------------------------------------------------------------

const DEEP_DIVE_PANELS: {
  id: ActivePanel
  label: string
  icon: React.ReactNode
  minRole: string
  description: string
}[] = [
  { id: 'graph',      label: 'Execution Graph', icon: <GitBranch size={14} />,  minRole: 'viewer',   description: 'Full causal DAG' },
  { id: 'evidence',   label: 'Evidence',         icon: <FileText size={14} />,   minRole: 'viewer',   description: 'Raw tool outputs' },
  { id: 'memory',     label: 'Memory Trace',     icon: <Brain size={14} />,      minRole: 'viewer',   description: 'STM / LTM matches' },
  { id: 'tools',      label: 'Tool Inspector',   icon: <Wrench size={14} />,     minRole: 'viewer',   description: 'Signal → hypothesis influence' },
  { id: 'reflection', label: 'Self-Awareness',   icon: <Sparkles size={14} />,   minRole: 'viewer',   description: 'Harness quality rounds' },
  { id: 'replay',     label: 'Replay',           icon: <RotateCcw size={14} />,  minRole: 'operator', description: 'Step-through investigation' },
  { id: 'control',    label: 'Control',          icon: <Shield size={14} />,     minRole: 'operator', description: 'Approve / override actions' },
]

// ---------------------------------------------------------------------------
// Nav link helper
// ---------------------------------------------------------------------------

function GlobalNavLink({
  to,
  icon,
  label,
  badge,
}: {
  to: string
  icon: React.ReactNode
  label: string
  badge?: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        clsx(
          'flex items-center gap-2.5 px-3 py-2 mx-1 rounded text-sm transition-colors',
          isActive
            ? 'bg-blue-900/40 text-blue-300'
            : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
        )
      }
    >
      {icon}
      <span className="flex-1 text-[13px]">{label}</span>
      {badge}
    </NavLink>
  )
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

export function Sidebar() {
  const { investigationId } = useParams<{ investigationId?: string }>()
  const { activePanel, setActivePanel, wsStatus, pendingApproval, harnessPhase, investigation } =
    useInvestigationStore()
  const { hasRole } = useAuthStore()

  const wsColor = {
    connected:    'bg-green-500',
    connecting:   'bg-yellow-500 animate-pulse',
    disconnected: 'bg-slate-600',
    error:        'bg-red-500',
  }[wsStatus]

  return (
    <aside className="w-52 bg-slate-900 border-r border-slate-800 flex flex-col shrink-0">

      {/* Logo */}
      <div className="px-4 py-3.5 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 bg-blue-600 rounded-md flex items-center justify-center text-white text-[11px] font-bold shrink-0">
            AI
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-100 leading-tight">SentinalAI</div>
            <div className="text-[10px] text-slate-500">Neo SRE Agent</div>
          </div>
        </div>
      </div>

      {/* Global navigation */}
      <nav className="py-2">
        <div className="px-3 py-1.5 text-[10px] text-slate-600 uppercase tracking-wider">
          Global
        </div>

        <GlobalNavLink
          to="/investigations"
          icon={<LayoutDashboard size={14} />}
          label="Mission Control"
        />
        <GlobalNavLink
          to="/dashboard"
          icon={<BarChart2 size={14} />}
          label="MTTI · MTTR"
        />
        <GlobalNavLink
          to="/architecture"
          icon={<Network size={14} />}
          label="Neural Model"
        />
      </nav>

      {/* Investigation deep-dives — shown when inside an investigation */}
      {investigationId && (
        <>
          <div className="border-t border-slate-800 mt-1" />
          <nav className="py-2 flex-1">
            {/* Back to timeline / 3-col view */}
            <div className="px-3 py-1.5 text-[10px] text-slate-600 uppercase tracking-wider flex items-center gap-1">
              <Zap size={9} />
              Deep Dives
              {investigation && (
                <span className="ml-auto font-mono text-slate-700 text-[9px] truncate max-w-20">
                  {investigation.incident_id}
                </span>
              )}
            </div>

            {/* Timeline = main 3-col view */}
            <button
              onClick={() => setActivePanel('timeline')}
              className={clsx(
                'w-full flex items-center gap-2.5 px-3 py-2 mx-0 text-sm transition-colors text-left',
                activePanel === 'timeline'
                  ? 'bg-blue-900/40 text-blue-300'
                  : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
              )}
            >
              <LayoutDashboard size={14} />
              <span className="text-[13px]">Overview</span>
              <span className="ml-auto text-[9px] text-slate-700">3-col</span>
            </button>

            {DEEP_DIVE_PANELS.map((panel) => {
              const enabled = hasRole(panel.minRole as 'viewer' | 'operator')
              return (
                <button
                  key={panel.id}
                  disabled={!enabled}
                  onClick={() => enabled && setActivePanel(panel.id)}
                  title={panel.description}
                  className={clsx(
                    'w-full flex items-center gap-2.5 px-3 py-2 text-sm transition-colors text-left',
                    activePanel === panel.id
                      ? 'bg-blue-900/40 text-blue-300'
                      : enabled
                      ? 'text-slate-500 hover:text-slate-200 hover:bg-slate-800'
                      : 'text-slate-700 cursor-not-allowed'
                  )}
                >
                  {panel.icon}
                  <span className="text-[13px] flex-1">{panel.label}</span>

                  {/* Pending approval badge */}
                  {panel.id === 'control' && pendingApproval && (
                    <AlertTriangle size={11} className="text-yellow-400 animate-pulse" />
                  )}
                  {/* Harness active pulse */}
                  {panel.id === 'reflection' &&
                    harnessPhase &&
                    harnessPhase !== 'reflection_complete' && (
                      <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
                    )}
                </button>
              )
            })}
          </nav>
        </>
      )}

      {/* WS status */}
      <div className="px-4 py-3 border-t border-slate-800 mt-auto">
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
          <span className={clsx('w-2 h-2 rounded-full shrink-0', wsColor)} />
          <span className="capitalize">{wsStatus}</span>
        </div>
      </div>
    </aside>
  )
}
