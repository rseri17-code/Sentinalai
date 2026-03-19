import React from 'react'
import { ExternalLink, Copy, RefreshCw } from 'lucide-react'
import { useInvestigationStore, useAuthStore } from '@/store/investigationStore'
import clsx from 'clsx'

const STATUS_COLORS: Record<string, string> = {
  running: 'text-blue-400 bg-blue-900/30 border-blue-800',
  completed: 'text-green-400 bg-green-900/30 border-green-800',
  failed: 'text-red-400 bg-red-900/30 border-red-800',
  pending: 'text-slate-400 bg-slate-800 border-slate-700',
  paused: 'text-yellow-400 bg-yellow-900/30 border-yellow-800',
  awaiting_approval: 'text-purple-400 bg-purple-900/30 border-purple-800',
}

export function TopBar() {
  const { investigation, wsStatus, refreshInvestigation } = useInvestigationStore()
  const { user } = useAuthStore()
  const [copied, setCopied] = React.useState(false)

  const copyTraceId = () => {
    if (!investigation?.trace_id) return
    navigator.clipboard.writeText(investigation.trace_id)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center px-4 gap-4 shrink-0">
      {investigation ? (
        <>
          {/* Incident ID */}
          <span className="font-mono text-xs text-blue-400 bg-blue-900/20 border border-blue-800/50 px-2 py-1 rounded">
            {investigation.incident_id}
          </span>

          {/* Summary */}
          <span className="text-sm text-slate-300 truncate max-w-md" title={investigation.summary}>
            {investigation.summary || investigation.affected_service}
          </span>

          {/* Status */}
          <span className={clsx('badge border', STATUS_COLORS[investigation.status] || STATUS_COLORS.pending)}>
            {investigation.status === 'running' && (
              <span className="w-1.5 h-1.5 bg-blue-400 rounded-full mr-1.5 animate-pulse inline-block" />
            )}
            {investigation.status}
          </span>

          {/* Trace ID */}
          {investigation.trace_id && (
            <div className="flex items-center gap-1 ml-auto">
              <span className="text-xs text-slate-500">trace:</span>
              <span className="font-mono text-xs text-slate-400">
                {investigation.trace_id.slice(0, 24)}...
              </span>
              <button onClick={copyTraceId} className="text-slate-500 hover:text-slate-300">
                <Copy size={12} />
              </button>
              {copied && <span className="text-xs text-green-400">Copied!</span>}
              {investigation.x_ray_trace_url && (
                <a
                  href={investigation.x_ray_trace_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-slate-500 hover:text-blue-400"
                  title="View in X-Ray"
                >
                  <ExternalLink size={12} />
                </a>
              )}
            </div>
          )}

          {/* Refresh */}
          <button
            onClick={refreshInvestigation}
            className="ml-2 text-slate-500 hover:text-slate-300"
            title="Refresh"
          >
            <RefreshCw size={14} />
          </button>
        </>
      ) : (
        <span className="text-sm text-slate-500">Select an investigation</span>
      )}

      {/* User */}
      {user && (
        <div className="ml-auto flex items-center gap-2 text-xs text-slate-500">
          <span className="bg-slate-800 px-2 py-1 rounded border border-slate-700">
            {user.role}
          </span>
          <span>{user.actor_id}</span>
        </div>
      )}
    </header>
  )
}
