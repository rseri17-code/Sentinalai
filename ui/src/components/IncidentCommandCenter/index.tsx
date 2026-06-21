// Panel 1: Incident Command Center
// Shows real-time event timeline + agent decision overlay

import React, { useEffect, useRef } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import type { AGUIEvent } from '@/types'
import clsx from 'clsx'
import { formatDistanceToNow } from 'date-fns'

const EVENT_COLORS: Record<string, string> = {
  'investigation.started': 'border-blue-500 bg-blue-900/20',
  'investigation.completed': 'border-green-500 bg-green-900/20',
  'investigation.failed': 'border-red-500 bg-red-900/20',
  'investigation.paused': 'border-yellow-500 bg-yellow-900/20',
  'tool.called': 'border-slate-600 bg-slate-800/50',
  'tool.responded': 'border-slate-600 bg-slate-800/30',
  'tool.failed': 'border-red-700 bg-red-900/20',
  'hypothesis.scored': 'border-purple-600 bg-purple-900/20',
  'hypothesis.selected': 'border-purple-500 bg-purple-900/30',
  'llm.invoked': 'border-indigo-600 bg-indigo-900/20',
  'llm.responded': 'border-indigo-500 bg-indigo-900/20',
  'rca.generated': 'border-green-500 bg-green-900/30',
  'control.requested': 'border-yellow-500 bg-yellow-900/30 animate-pulse',
  'control.approved': 'border-green-500 bg-green-900/20',
  'control.rejected': 'border-red-500 bg-red-900/20',
  'budget.warning': 'border-orange-500 bg-orange-900/20',
  'circuit_breaker.tripped': 'border-red-600 bg-red-900/30',
  'memory.queried': 'border-cyan-700 bg-cyan-900/20',
  'incident.classified': 'border-slate-500 bg-slate-800/50',
  'playbook.selected': 'border-slate-500 bg-slate-800/50',
}

const EVENT_ICONS: Record<string, string> = {
  'investigation.started': '🚀',
  'investigation.completed': '✅',
  'investigation.failed': '❌',
  'investigation.paused': '⏸',
  'tool.called': '⚙️',
  'tool.responded': '↩',
  'tool.failed': '💥',
  'hypothesis.scored': '🧠',
  'hypothesis.selected': '🎯',
  'llm.invoked': '🤖',
  'llm.responded': '💬',
  'rca.generated': '📋',
  'control.requested': '🔐',
  'control.approved': '✅',
  'control.rejected': '🚫',
  'budget.warning': '⚠️',
  'circuit_breaker.tripped': '🔌',
  'memory.queried': '🧬',
  'incident.classified': '🔍',
  'playbook.selected': '📖',
}

function EventCard({ event }: { event: AGUIEvent }) {
  const isReplay = (event.payload as Record<string, unknown>)._replay === true

  return (
    <div
      className={clsx(
        'border-l-2 pl-3 py-2 pr-2 rounded-r text-xs',
        EVENT_COLORS[event.event_type] || 'border-slate-700 bg-slate-800/30'
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-sm shrink-0">{EVENT_ICONS[event.event_type] || '•'}</span>
          <span className="font-mono text-slate-300 truncate">{event.event_type}</span>
          {isReplay && (
            <span className="badge bg-purple-900/50 text-purple-400 shrink-0">replay</span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0 text-slate-500">
          <span className="font-mono">#{event.sequence_num}</span>
          <span>
            {formatDistanceToNow(new Date(event.timestamp), { addSuffix: true })}
          </span>
        </div>
      </div>

      {/* Payload summary */}
      <EventPayloadSummary event={event} />
    </div>
  )
}

function EventPayloadSummary({ event }: { event: AGUIEvent }) {
  const p = event.payload as Record<string, unknown>

  switch (event.event_type) {
    case 'tool.called':
      return (
        <div className="mt-1 text-slate-400">
          <span className="text-slate-300">{p.worker as string}</span>
          <span className="text-slate-600"> → </span>
          <span>{p.action as string}</span>
        </div>
      )
    case 'tool.responded':
      return (
        <div className="mt-1 text-slate-400">
          <span className="text-slate-300">{p.worker as string}</span>
          <span className="text-slate-600"> • </span>
          <span className={p.status === 'success' ? 'text-green-400' : 'text-red-400'}>
            {p.status as string}
          </span>
          {typeof p.elapsed_ms === 'number' && (
            <span className="text-slate-500"> • {(p.elapsed_ms as number).toFixed(0)}ms</span>
          )}
          {typeof p.result_count === 'number' && (
            <span className="text-slate-500"> • {p.result_count as number} results</span>
          )}
        </div>
      )
    case 'hypothesis.scored':
      return (
        <div className="mt-1 text-slate-400">
          Winner: <span className="text-purple-300">{p.winner as string}</span>
          {' '}
          <span className="text-purple-400">{((p.confidence as number) * 100).toFixed(0)}%</span>
        </div>
      )
    case 'rca.generated':
      return (
        <div className="mt-1 text-slate-300 line-clamp-2">
          {p.root_cause as string}
        </div>
      )
    case 'control.requested':
      return (
        <div className="mt-1 text-yellow-300 font-medium">
          ⏸ Awaiting approval: {p.reason as string}
        </div>
      )
    case 'budget.warning':
      return (
        <div className="mt-1 text-orange-400">
          {p.calls_used as number}/{p.calls_max as number} calls used
        </div>
      )
    case 'llm.invoked':
      return (
        <div className="mt-1 text-slate-400">
          {p.model as string} • {p.purpose as string} • {p.input_tokens as number} tokens
        </div>
      )
    default:
      return null
  }
}

function AgentDecisionOverlay() {
  const { investigation } = useInvestigationStore()
  if (!investigation?.root_cause && !investigation?.winner_hypothesis) return null

  return (
    <div className="panel p-4 space-y-3">
      <div className="text-xs uppercase text-slate-500 tracking-wider font-medium">
        Agent Decision
      </div>

      {investigation.winner_hypothesis && (
        <div>
          <div className="text-xs text-slate-500 mb-1">Selected Hypothesis</div>
          <div className="text-sm text-purple-300 font-medium">
            {investigation.winner_hypothesis}
          </div>
        </div>
      )}

      {investigation.root_cause && (
        <div>
          <div className="text-xs text-slate-500 mb-1">Root Cause</div>
          <div className="text-sm text-slate-200">{investigation.root_cause}</div>
        </div>
      )}

      {/* Hypothesis comparison */}
      {investigation.hypotheses.length > 0 && (
        <div>
          <div className="text-xs text-slate-500 mb-2">All Hypotheses</div>
          <div className="space-y-1.5">
            {[...investigation.hypotheses]
              .sort((a, b) => b.score - a.score)
              .map((h) => (
                <div key={h.name} className="flex items-center gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      {h.is_winner && <span className="text-xs text-yellow-400">★</span>}
                      <span className={clsx('text-xs truncate', h.is_winner ? 'text-purple-300' : 'text-slate-400')}>
                        {h.name}
                      </span>
                    </div>
                    <div className="mt-0.5 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                      <div
                        className={clsx('h-full rounded-full transition-all', h.is_winner ? 'bg-purple-500' : 'bg-slate-600')}
                        style={{ width: `${h.score * 100}%` }}
                      />
                    </div>
                  </div>
                  <span className={clsx('text-xs font-mono w-10 text-right shrink-0', h.is_winner ? 'text-purple-400' : 'text-slate-500')}>
                    {(h.score * 100).toFixed(0)}%
                  </span>
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  )
}

export function IncidentCommandCenter() {
  const { events, investigation } = useInvestigationStore()
  const bottomRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to latest event
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [events.length])

  return (
    <div className="flex h-full gap-4 p-4 overflow-hidden">
      {/* Event Timeline */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <div className="panel-header rounded-t-lg bg-slate-900 border border-slate-800">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-slate-200">Event Timeline</span>
            <span className="badge bg-slate-800 text-slate-400">{events.length} events</span>
          </div>
          {investigation?.status === 'running' && (
            <div className="flex items-center gap-1.5 text-xs text-blue-400">
              <span className="w-1.5 h-1.5 bg-blue-400 rounded-full animate-pulse" />
              Live
            </div>
          )}
        </div>
        <div className="flex-1 scroll-panel border border-t-0 border-slate-800 rounded-b-lg bg-slate-900 p-3 space-y-2">
          {events.length === 0 ? (
            <div className="text-center text-slate-600 py-8 text-sm">
              Waiting for events...
            </div>
          ) : (
            events.map((event) => (
              <EventCard key={event.event_id} event={event} />
            ))
          )}
          <div ref={bottomRef} />
        </div>
      </div>

      {/* Agent Decision Overlay */}
      <div className="w-72 shrink-0 space-y-3 overflow-y-auto">
        <AgentDecisionOverlay />

        {/* Incident metadata */}
        {investigation && (
          <div className="panel p-3 space-y-2 text-xs">
            <div className="text-slate-500 uppercase tracking-wider font-medium">Incident</div>
            <div className="space-y-1.5">
              <Row label="Service" value={investigation.affected_service} />
              <Row label="Type" value={investigation.incident_type} />
              <Row label="Severity" value={investigation.severity} />
              <Row label="Source" value={investigation.source} />
              {investigation.duration_ms && (
                <Row label="Duration" value={`${(investigation.duration_ms / 1000).toFixed(1)}s`} />
              )}
              <Row label="Playbook" value={(investigation.playbook ?? []).join(', ')} />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-300 truncate max-w-[130px]" title={value}>{value || '—'}</span>
    </div>
  )
}
