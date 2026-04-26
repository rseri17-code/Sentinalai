// Shift Handoff Panel — intelligence brief for incoming SRE shift
// Shows fragile services, active investigations, IF/THEN guidance, and upcoming changes.
// The incoming engineer clicks "Accept Shift" to acknowledge the brief.
// Designed for maximum clarity at the start of shift — no noise, all signal.

import React, { useEffect, useState, useCallback } from 'react'
import {
  ArrowRight, AlertTriangle, Activity, Calendar, Shield,
  CheckCircle, ChevronDown, ChevronRight, Clock, User
} from 'lucide-react'
import { formatDistanceToNow, format, parseISO } from 'date-fns'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface FragileService {
  service: string
  reason: string
  incident_count_7d: number
  last_incident_type: string
  risk_level: 'elevated' | 'high' | 'critical'
  watch_signals: string[]
}

interface ConditionalGuidance {
  trigger: string
  action: string
  escalate_to: string
  runbook_hint: string
}

interface UpcomingChange {
  service: string
  change_type: string
  scheduled_at: string
  risk_level: 'low' | 'medium' | 'high'
  description: string
}

interface HandoffBrief {
  generated_at: string
  shift_start: string
  outgoing_engineer: string
  incoming_engineer: string
  fragile_services: FragileService[]
  active_investigations: { incident_id: string; status: string }[]
  watch_items: string[]
  upcoming_risk: UpcomingChange[]
  conditional_guidance: ConditionalGuidance[]
  open_action_items: unknown[]
  summary: string
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const RISK_CONFIG = {
  elevated: { color: 'text-blue-400',   bg: 'bg-blue-900/20',   border: 'border-blue-700',   dot: 'bg-blue-400' },
  high:     { color: 'text-orange-400', bg: 'bg-orange-900/20', border: 'border-orange-700', dot: 'bg-orange-400' },
  critical: { color: 'text-red-400',    bg: 'bg-red-900/30',    border: 'border-red-700',    dot: 'bg-red-400' },
}

const CHANGE_RISK_CONFIG = {
  low:    { color: 'text-green-400',  bg: 'bg-green-900/20',  label: 'Low' },
  medium: { color: 'text-yellow-400', bg: 'bg-yellow-900/20', label: 'Medium' },
  high:   { color: 'text-red-400',    bg: 'bg-red-900/30',    label: 'High' },
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function RiskBadge({ level }: { level: 'elevated' | 'high' | 'critical' }) {
  const cfg = RISK_CONFIG[level]
  return (
    <span className={clsx('flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-bold uppercase', cfg.color, cfg.bg)}>
      <span className={clsx('h-1.5 w-1.5 rounded-full', cfg.dot)} />
      {level}
    </span>
  )
}

function FragileServiceCard({ svc }: { svc: FragileService }) {
  const [expanded, setExpanded] = useState(svc.risk_level === 'critical')
  const cfg = RISK_CONFIG[svc.risk_level]
  return (
    <div className={clsx('rounded-xl border overflow-hidden', cfg.bg, cfg.border)}>
      <button
        className="w-full flex items-center gap-3 px-4 py-3 hover:bg-slate-800/30 transition-colors text-left"
        onClick={() => setExpanded(e => !e)}
      >
        <RiskBadge level={svc.risk_level} />
        <span className="flex-1 font-mono text-sm font-semibold text-slate-100">{svc.service}</span>
        <span className={clsx('text-xs font-bold', cfg.color)}>
          {svc.incident_count_7d}x this week
        </span>
        {expanded ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
      </button>
      {expanded && (
        <div className="px-4 pb-4 space-y-3">
          <p className="text-xs text-slate-400">{svc.reason}</p>
          {svc.watch_signals.length > 0 && (
            <div>
              <p className="text-xs font-medium text-slate-400 mb-1.5">Watch these signals:</p>
              <div className="flex flex-wrap gap-1.5">
                {svc.watch_signals.map((signal, i) => (
                  <span key={i} className="text-xs bg-slate-800 text-slate-300 rounded px-2 py-0.5 border border-slate-700">
                    {signal}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function GuidanceCard({ guidance }: { guidance: ConditionalGuidance }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="border border-slate-700 rounded-xl overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-4 py-3 hover:bg-slate-800 transition-colors text-left"
        onClick={() => setExpanded(e => !e)}
      >
        <AlertTriangle className="h-4 w-4 text-yellow-500 flex-shrink-0" />
        <span className="flex-1 text-xs text-slate-300 line-clamp-1">{guidance.trigger}</span>
        {expanded ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
      </button>
      {expanded && (
        <div className="px-4 pb-4 space-y-2 bg-slate-900/40">
          <div className="mt-2">
            <p className="text-xs font-bold text-yellow-500 uppercase mb-1">If</p>
            <p className="text-xs text-slate-300">{guidance.trigger}</p>
          </div>
          <div>
            <p className="text-xs font-bold text-green-500 uppercase mb-1">Then</p>
            <p className="text-xs text-slate-300">{guidance.action}</p>
          </div>
          {guidance.runbook_hint && (
            <p className="text-xs text-blue-400">{guidance.runbook_hint}</p>
          )}
          {guidance.escalate_to && (
            <p className="text-xs text-slate-500">Escalate to: {guidance.escalate_to}</p>
          )}
        </div>
      )}
    </div>
  )
}

function ChangeRow({ change }: { change: UpcomingChange }) {
  const cfg = CHANGE_RISK_CONFIG[change.risk_level] ?? CHANGE_RISK_CONFIG.medium
  const scheduled = (() => {
    try { return format(parseISO(change.scheduled_at), 'HH:mm') } catch { return change.scheduled_at }
  })()
  return (
    <div className="flex items-center gap-3 py-2 border-b border-slate-800 last:border-0">
      <span className={clsx('text-xs font-bold uppercase px-2 py-0.5 rounded', cfg.color, cfg.bg)}>
        {cfg.label}
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-xs font-mono text-slate-200 truncate">{change.service}</p>
        <p className="text-xs text-slate-500">{change.change_type}</p>
      </div>
      <div className="text-right flex-shrink-0">
        <p className="text-xs font-mono text-slate-300">{scheduled}</p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface Props {
  outgoingEngineer?: string
  incomingEngineer?: string
}

export default function ShiftHandoffPanel({ outgoingEngineer = '', incomingEngineer = '' }: Props) {
  const [brief, setBrief] = useState<HandoffBrief | null>(null)
  const [loading, setLoading] = useState(false)
  const [accepted, setAccepted] = useState(false)
  const [error, setError] = useState('')

  const token = localStorage.getItem('agui_token')
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }

  const fetchBrief = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/api/v1/handoff/current', {
        method: 'GET',
        headers,
      })
      if (!res.ok) throw new Error(`${res.status}`)
      const data: HandoffBrief = await res.json()
      setBrief(data)
    } catch {
      // Try generating
      try {
        const res = await fetch('/api/v1/handoff', {
          method: 'POST',
          headers,
          body: JSON.stringify({
            outgoing_engineer: outgoingEngineer || 'outgoing-sre',
            incoming_engineer: incomingEngineer || 'incoming-sre',
          }),
        })
        if (!res.ok) throw new Error(`${res.status}`)
        const data: HandoffBrief = await res.json()
        setBrief(data)
      } catch (e) {
        setError(String(e))
      }
    } finally {
      setLoading(false)
    }
  }, [outgoingEngineer, incomingEngineer])

  useEffect(() => { fetchBrief() }, [fetchBrief])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-40 text-slate-500 text-sm">
        Generating shift handoff brief...
      </div>
    )
  }

  if (error || !brief) {
    return (
      <div className="flex flex-col items-center justify-center h-40 gap-3">
        <p className="text-sm text-slate-500">{error || 'No handoff brief available'}</p>
        <button onClick={fetchBrief} className="px-4 py-2 bg-blue-700 hover:bg-blue-600 text-white text-sm rounded-lg">
          Generate Brief
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-4 border-b border-slate-700">
        <div className="flex items-center gap-3 mb-3">
          <div className="flex items-center gap-2">
            <User className="h-4 w-4 text-slate-400" />
            <span className="text-sm font-medium text-slate-200">{brief.outgoing_engineer}</span>
          </div>
          <ArrowRight className="h-4 w-4 text-slate-500" />
          <div className="flex items-center gap-2">
            <User className="h-4 w-4 text-blue-400" />
            <span className="text-sm font-medium text-blue-300">{brief.incoming_engineer}</span>
          </div>
          <span className="ml-auto text-xs text-slate-500">
            {formatDistanceToNow(new Date(brief.generated_at), { addSuffix: true })}
          </span>
        </div>

        {/* Summary */}
        <p className="text-xs text-slate-400 leading-relaxed">{brief.summary}</p>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5">

        {/* Fragile Services */}
        {brief.fragile_services.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <AlertTriangle className="h-4 w-4 text-orange-400" />
              <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wide">
                Fragile Services ({brief.fragile_services.length})
              </h3>
            </div>
            <div className="space-y-2">
              {brief.fragile_services.map(svc => (
                <FragileServiceCard key={svc.service} svc={svc} />
              ))}
            </div>
          </div>
        )}

        {/* Active Investigations */}
        {brief.active_investigations.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <Activity className="h-4 w-4 text-blue-400" />
              <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wide">
                Active Investigations ({brief.active_investigations.length})
              </h3>
            </div>
            <div className="space-y-1.5">
              {brief.active_investigations.map(inv => (
                <div key={inv.incident_id} className="flex items-center gap-3 bg-slate-800 rounded-lg px-3 py-2">
                  <span className="w-2 h-2 rounded-full bg-blue-400 flex-shrink-0 animate-pulse" />
                  <span className="font-mono text-sm text-slate-200">{inv.incident_id}</span>
                  <span className="ml-auto text-xs text-slate-500">{inv.status}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* IF/THEN Guidance */}
        {brief.conditional_guidance.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <Shield className="h-4 w-4 text-yellow-400" />
              <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wide">
                If/Then Playbook
              </h3>
            </div>
            <div className="space-y-2">
              {brief.conditional_guidance.map((g, i) => (
                <GuidanceCard key={i} guidance={g} />
              ))}
            </div>
          </div>
        )}

        {/* Upcoming Changes */}
        {brief.upcoming_risk.length > 0 && (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <Calendar className="h-4 w-4 text-slate-400" />
              <h3 className="text-xs font-bold text-slate-300 uppercase tracking-wide">
                Upcoming Changes ({brief.upcoming_risk.length})
              </h3>
            </div>
            <div className="bg-slate-800/60 rounded-xl border border-slate-700 divide-y divide-slate-700 px-3">
              {brief.upcoming_risk.map((c, i) => (
                <ChangeRow key={i} change={c} />
              ))}
            </div>
          </div>
        )}

        {/* Accept Shift */}
        <div className="pt-2 pb-4">
          {accepted ? (
            <div className="flex items-center justify-center gap-2 py-3 bg-green-900/20 border border-green-700 rounded-xl">
              <CheckCircle className="h-5 w-5 text-green-400" />
              <span className="text-sm font-medium text-green-300">Shift accepted — you have the context</span>
            </div>
          ) : (
            <button
              onClick={() => setAccepted(true)}
              className="w-full py-3 bg-blue-700 hover:bg-blue-600 active:bg-blue-800
                         text-white font-semibold text-sm rounded-xl transition-colors
                         flex items-center justify-center gap-2"
            >
              <CheckCircle className="h-4 w-4" />
              Accept Shift — I've read the brief
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
