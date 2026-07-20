import React from 'react'
import { NavLink, useParams } from 'react-router-dom'
import {
  Activity, GitBranch, FileText, Brain, RotateCcw, Shield, AlertTriangle, BarChart2, Sparkles, Wrench, Share2, HeartPulse
} from 'lucide-react'
import { useInvestigationStore, useAuthStore } from '@/store/investigationStore'
import type { ActivePanel } from '@/types'
import clsx from 'clsx'

const PANELS: { id: ActivePanel; label: string; icon: React.ReactNode; minRole: string }[] = [
  { id: 'timeline', label: 'Timeline', icon: <Activity size={16} />, minRole: 'viewer' },
  { id: 'graph', label: 'Execution Graph', icon: <GitBranch size={16} />, minRole: 'viewer' },
  { id: 'evidence', label: 'Evidence', icon: <FileText size={16} />, minRole: 'viewer' },
  { id: 'memory', label: 'Memory Trace', icon: <Brain size={16} />, minRole: 'viewer' },
  { id: 'reflection', label: 'Self-Awareness', icon: <Sparkles size={16} />, minRole: 'viewer' },
  { id: 'tools', label: 'Tool Inspector', icon: <Wrench size={16} />, minRole: 'viewer' },
  { id: 'replay', label: 'Replay', icon: <RotateCcw size={16} />, minRole: 'operator' },
  { id: 'control', label: 'Control', icon: <Shield size={16} />, minRole: 'operator' },
]

export function Sidebar() {
  const { investigationId } = useParams<{ investigationId?: string }>()
  const { activePanel, setActivePanel, wsStatus, pendingApproval, harnessPhase } = useInvestigationStore()
  const { hasRole } = useAuthStore()

  const wsColor = {
    connected: 'bg-green-500',
    connecting: 'bg-yellow-500 animate-pulse',
    disconnected: 'bg-slate-600',
    error: 'bg-red-500',
  }[wsStatus]

  return (
    <aside className="w-56 bg-slate-900 border-r border-slate-800 flex flex-col">
      {/* Logo */}
      <div className="px-4 py-4 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 bg-blue-600 rounded-md flex items-center justify-center text-white text-xs font-bold">
            AI
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-100">SentinalAI</div>
            <div className="text-xs text-slate-500">Agent Control Plane</div>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-2">
        <div className="px-3 py-1.5 text-xs text-slate-500 uppercase tracking-wider">
          Navigation
        </div>
        <NavLink
          to="/investigations"
          className={({ isActive }) =>
            clsx(
              'flex items-center gap-2 px-3 py-2 mx-1 rounded text-sm transition-colors',
              isActive
                ? 'bg-blue-900/40 text-blue-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            )
          }
        >
          <Activity size={15} />
          All Investigations
        </NavLink>
        <NavLink
          to="/dashboard"
          className={({ isActive }) =>
            clsx(
              'flex items-center gap-2 px-3 py-2 mx-1 rounded text-sm transition-colors',
              isActive
                ? 'bg-blue-900/40 text-blue-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            )
          }
        >
          <BarChart2 size={15} />
          MTTR Dashboard
        </NavLink>
        <NavLink
          to="/operational-health"
          className={({ isActive }) =>
            clsx(
              'flex items-center gap-2 px-3 py-2 mx-1 rounded text-sm transition-colors',
              isActive
                ? 'bg-blue-900/40 text-blue-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            )
          }
        >
          <HeartPulse size={15} />
          Operational Health
        </NavLink>
        <NavLink
          to="/graph"
          className={({ isActive }) =>
            clsx(
              'flex items-center gap-2 px-3 py-2 mx-1 rounded text-sm transition-colors',
              isActive
                ? 'bg-blue-900/40 text-blue-400'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
            )
          }
        >
          <Share2 size={16} />
          Knowledge Graph
        </NavLink>

        {/* Investigation panels — only shown when viewing an investigation */}
        {investigationId && (
          <>
            <div className="px-3 py-1.5 mt-3 text-xs text-slate-500 uppercase tracking-wider">
              Investigation
            </div>
            {PANELS.map((panel) => {
              const enabled = hasRole(panel.minRole as 'viewer' | 'operator')
              return (
                <button
                  key={panel.id}
                  disabled={!enabled}
                  onClick={() => enabled && setActivePanel(panel.id)}
                  className={clsx(
                    'w-full flex items-center gap-2 px-3 py-2 mx-1 rounded text-sm transition-colors text-left',
                    activePanel === panel.id
                      ? 'bg-blue-900/40 text-blue-400'
                      : enabled
                      ? 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
                      : 'text-slate-700 cursor-not-allowed'
                  )}
                >
                  {panel.icon}
                  <span className="flex-1">{panel.label}</span>
                  {/* Pending approval indicator */}
                  {panel.id === 'control' && pendingApproval && (
                    <AlertTriangle size={12} className="text-yellow-400 animate-pulse" />
                  )}
                  {/* Harness active indicator */}
                  {panel.id === 'reflection' && harnessPhase && harnessPhase !== 'reflection_complete' && (
                    <span className="w-1.5 h-1.5 rounded-full bg-violet-400 animate-pulse" />
                  )}
                </button>
              )
            })}
          </>
        )}
      </nav>

      {/* WS Status */}
      <div className="px-4 py-3 border-t border-slate-800">
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className={clsx('w-2 h-2 rounded-full', wsColor)} />
          <span className="capitalize">{wsStatus}</span>
        </div>
      </div>
    </aside>
  )
}
