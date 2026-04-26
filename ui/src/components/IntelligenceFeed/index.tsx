// Intelligence Feed — proactive pre-incident signal cards
// Shows signals from the sentinel loop BEFORE PagerDuty fires.
// The feed is the face of SentinalAI's proactive detection capability.

import React, { useEffect, useState, useCallback } from 'react'
import { TrendingUp, AlertTriangle, Activity, CheckCheck, Search } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Urgency = 'WATCH' | 'WARNING' | 'IMMINENT' | 'BREACHED'

interface SignalAlert {
  id: string
  service: string
  metric_name: string
  current_value: number
  threshold: number
  urgency: Urgency
  trend_direction: 'rising' | 'falling' | 'stable'
  minutes_to_breach: number | null
  recommended_action: string
  confidence: number
  detected_at: string
  acknowledged_by?: string
  acknowledged_at?: string
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const URGENCY_CONFIG: Record<Urgency, {
  color: string; bg: string; border: string; label: string; pulse: boolean
}> = {
  WATCH:    { color: 'text-blue-400',   bg: 'bg-blue-900/20',   border: 'border-blue-700',   label: 'Watch',    pulse: false },
  WARNING:  { color: 'text-yellow-400', bg: 'bg-yellow-900/20', border: 'border-yellow-700', label: 'Warning',  pulse: false },
  IMMINENT: { color: 'text-orange-400', bg: 'bg-orange-900/30', border: 'border-orange-600', label: 'Imminent', pulse: true  },
  BREACHED: { color: 'text-red-400',    bg: 'bg-red-900/30',    border: 'border-red-600',    label: 'Breached', pulse: true  },
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function UrgencyBadge({ urgency }: { urgency: Urgency }) {
  const cfg = URGENCY_CONFIG[urgency] ?? URGENCY_CONFIG.WATCH
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold uppercase',
      cfg.color, cfg.bg
    )}>
      {cfg.pulse && (
        <span className="relative flex h-2 w-2">
          <span className={clsx('animate-ping absolute inline-flex h-full w-full rounded-full opacity-75',
            urgency === 'BREACHED' ? 'bg-red-400' : 'bg-orange-400')} />
          <span className={clsx('relative inline-flex rounded-full h-2 w-2',
            urgency === 'BREACHED' ? 'bg-red-500' : 'bg-orange-500')} />
        </span>
      )}
      {cfg.label}
    </span>
  )
}

function TrendIndicator({ direction, value, threshold }: {
  direction: string; value: number; threshold: number
}) {
  const pct = Math.min(100, (value / threshold) * 100)
  const color = pct >= 95 ? 'bg-red-500' : pct >= 80 ? 'bg-orange-500' :
                pct >= 60 ? 'bg-yellow-500' : 'bg-blue-500'

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-slate-400">{value.toFixed(1)}</span>
        <span className="text-slate-500">threshold: {threshold.toFixed(1)}</span>
      </div>
      <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div
          className={clsx('h-full rounded-full transition-all duration-700', color)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex items-center gap-1 text-xs text-slate-500">
        <TrendingUp className="h-3 w-3" />
        <span>{direction}</span>
        <span>({pct.toFixed(0)}% of threshold)</span>
      </div>
    </div>
  )
}

function AlertCard({ alert, onAcknowledge, onInvestigate }: {
  alert: SignalAlert
  onAcknowledge: (id: string) => void
  onInvestigate: (alert: SignalAlert) => void
}) {
  const cfg = URGENCY_CONFIG[alert.urgency] ?? URGENCY_CONFIG.WATCH
  const isAcknowledged = !!alert.acknowledged_by
  const breach = alert.minutes_to_breach

  return (
    <div className={clsx(
      'rounded-xl border p-4 space-y-3 transition-all',
      cfg.bg, cfg.border,
      isAcknowledged && 'opacity-60'
    )}>
      {/* Header row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <UrgencyBadge urgency={alert.urgency} />
            <span className="font-mono text-sm font-semibold text-slate-100 truncate">
              {alert.service}
            </span>
          </div>
          <p className="text-xs text-slate-400 mt-1">{alert.metric_name}</p>
        </div>
        <span className="text-xs text-slate-500 flex-shrink-0 mt-0.5">
          {formatDistanceToNow(new Date(alert.detected_at), { addSuffix: true })}
        </span>
      </div>

      {/* Trend bar */}
      <TrendIndicator
        direction={alert.trend_direction}
        value={alert.current_value}
        threshold={alert.threshold}
      />

      {/* Breach time */}
      {breach !== null && (
        <div className={clsx(
          'flex items-center gap-2 text-xs font-medium rounded px-2 py-1',
          alert.urgency === 'BREACHED' ? 'bg-red-900/40 text-red-300' : 'bg-slate-800 text-slate-300'
        )}>
          <AlertTriangle className="h-3 w-3 flex-shrink-0" />
          {alert.urgency === 'BREACHED'
            ? 'Threshold already breached'
            : `${Math.ceil(breach)} min to breach — detected before PagerDuty fires`}
        </div>
      )}

      {/* Recommended action */}
      {alert.recommended_action && (
        <p className="text-xs text-slate-400 leading-relaxed">
          <span className="text-slate-300 font-medium">Action: </span>
          {alert.recommended_action}
        </p>
      )}

      {/* Acknowledge state */}
      {isAcknowledged && (
        <div className="flex items-center gap-1.5 text-xs text-slate-500">
          <CheckCheck className="h-3 w-3" />
          Acknowledged by {alert.acknowledged_by}
        </div>
      )}

      {/* Actions */}
      {!isAcknowledged && (
        <div className="flex gap-2 pt-1">
          <button
            onClick={() => onInvestigate(alert)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-700 hover:bg-blue-600
                       text-white text-xs font-medium rounded-lg transition-colors"
          >
            <Search className="h-3 w-3" />
            Investigate
          </button>
          <button
            onClick={() => onAcknowledge(alert.id)}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-700 hover:bg-slate-600
                       text-slate-300 text-xs font-medium rounded-lg transition-colors"
          >
            <CheckCheck className="h-3 w-3" />
            Acknowledge
          </button>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface IntelligenceFeedProps {
  onInvestigate?: (service: string, metric: string) => void
}

export default function IntelligenceFeed({ onInvestigate }: IntelligenceFeedProps) {
  const [alerts, setAlerts] = useState<SignalAlert[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState<Urgency | 'ALL'>('ALL')
  const [lastRefresh, setLastRefresh] = useState(new Date())

  const fetchAlerts = useCallback(async () => {
    try {
      const token = localStorage.getItem('agui_token')
      const res = await fetch('/api/v1/intelligence/feed', {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) return
      const data = await res.json()
      setAlerts(data.alerts ?? [])
      setLastRefresh(new Date())
    } catch {
      // Silent — sentinel loop may not be running
    } finally {
      setLoading(false)
    }
  }, [])

  // Poll every 30s for new signals
  useEffect(() => {
    fetchAlerts()
    const id = setInterval(fetchAlerts, 30_000)
    return () => clearInterval(id)
  }, [fetchAlerts])

  const handleAcknowledge = useCallback(async (alertId: string) => {
    try {
      const token = localStorage.getItem('agui_token')
      await fetch(`/api/v1/intelligence/alerts/${alertId}/acknowledge`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ actor: 'operator' }),
      })
      setAlerts(prev =>
        prev.map(a =>
          a.id === alertId
            ? { ...a, acknowledged_by: 'you', acknowledged_at: new Date().toISOString() }
            : a
        )
      )
    } catch { /* silent */ }
  }, [])

  const handleInvestigate = useCallback((alert: SignalAlert) => {
    onInvestigate?.(alert.service, alert.metric_name)
  }, [onInvestigate])

  const urgencyCounts = alerts.reduce((acc, a) => {
    acc[a.urgency] = (acc[a.urgency] ?? 0) + 1
    return acc
  }, {} as Record<string, number>)

  const filtered = filter === 'ALL' ? alerts : alerts.filter(a => a.urgency === filter)
  const sortOrder: Record<string, number> = { BREACHED: 0, IMMINENT: 1, WARNING: 2, WATCH: 3 }
  const sorted = [...filtered].sort(
    (a, b) => (sortOrder[a.urgency] ?? 9) - (sortOrder[b.urgency] ?? 9)
  )

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-blue-400" />
          <span className="text-sm font-semibold text-slate-100">Intelligence Feed</span>
          {alerts.length > 0 && (
            <span className="text-xs bg-slate-700 text-slate-300 rounded-full px-2 py-0.5">
              {alerts.length}
            </span>
          )}
        </div>
        <span className="text-xs text-slate-500">
          Updated {formatDistanceToNow(lastRefresh, { addSuffix: true })}
        </span>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 px-4 py-2 border-b border-slate-700">
        {(['ALL', 'BREACHED', 'IMMINENT', 'WARNING', 'WATCH'] as const).map(u => {
          const count = u === 'ALL' ? alerts.length : (urgencyCounts[u] ?? 0)
          const cfg = u !== 'ALL' ? URGENCY_CONFIG[u] : null
          return (
            <button
              key={u}
              onClick={() => setFilter(u)}
              className={clsx(
                'flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium transition-colors',
                filter === u
                  ? (cfg ? clsx(cfg.color, cfg.bg) : 'bg-slate-600 text-slate-100')
                  : 'text-slate-500 hover:text-slate-300 hover:bg-slate-800'
              )}
            >
              {u}
              {count > 0 && (
                <span className={clsx(
                  'text-xs rounded-full px-1.5',
                  filter === u ? 'bg-slate-900/40' : 'bg-slate-700'
                )}>
                  {count}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* Feed */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {loading ? (
          <div className="flex items-center justify-center h-24 text-slate-500 text-sm">
            Loading signals...
          </div>
        ) : sorted.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 text-center">
            <CheckCheck className="h-8 w-8 text-green-500 mb-2" />
            <p className="text-sm text-slate-400">All clear — no anomalous signals</p>
            <p className="text-xs text-slate-600 mt-1">
              Sentinel loop is watching your services
            </p>
          </div>
        ) : (
          sorted.map(alert => (
            <AlertCard
              key={alert.id}
              alert={alert}
              onAcknowledge={handleAcknowledge}
              onInvestigate={handleInvestigate}
            />
          ))
        )}
      </div>
    </div>
  )
}
