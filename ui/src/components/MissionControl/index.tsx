// Mission Control — home page for the SRE agent
// Layout:
//   ┌─────────────────────────────────────────────────────────┐
//   │  Metrics strip: MTTI avg | MTTR avg | Active | Patterns │
//   ├────────────────────────────────┬────────────────────────┤
//   │  Investigation queue (70%)     │  Pattern Intel (30%)   │
//   └────────────────────────────────┴────────────────────────┘

import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, Clock, TrendingDown, AlertTriangle, Zap, RefreshCw, Plus } from 'lucide-react'
import { PatternIntelligencePanel } from '@/components/PatternIntelligencePanel'
import { fmtDuration, elapsedSince } from '@/utils/mtti'
import type { IncidentState } from '@/types'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types for MTTI/MTTR dashboard KPIs
// ---------------------------------------------------------------------------

interface KPIs {
  mttr_median_ms: number
  total_investigations: number
  last_24h_count: number
  root_cause_found_rate: number
  mean_confidence: number
}

// ---------------------------------------------------------------------------
// Metrics strip
// ---------------------------------------------------------------------------

function MetricPill({
  label,
  value,
  sub,
  trend,
  highlight,
}: {
  label: string
  value: string
  sub?: string
  trend?: 'up' | 'down' | 'neutral'
  highlight?: boolean
}) {
  return (
    <div className={clsx(
      'flex flex-col px-4 py-2 border-r border-slate-800 last:border-r-0',
      highlight && 'bg-blue-950/20'
    )}>
      <span className="text-[10px] text-slate-500 uppercase tracking-wider">{label}</span>
      <div className="flex items-baseline gap-1.5 mt-0.5">
        <span className={clsx('text-base font-semibold', highlight ? 'text-blue-300' : 'text-slate-200')}>
          {value}
        </span>
        {trend && (
          <TrendingDown
            size={11}
            className={clsx(
              trend === 'down' ? 'text-green-400' : trend === 'up' ? 'text-red-400 rotate-180' : 'text-slate-500'
            )}
          />
        )}
        {sub && <span className="text-[10px] text-slate-500">{sub}</span>}
      </div>
    </div>
  )
}

function MetricsStrip({ kpis, activeCount, patternAlertCount }: {
  kpis: KPIs | null
  activeCount: number
  patternAlertCount: number
}) {
  return (
    <div className="flex items-stretch border-b border-slate-800 bg-slate-900 shrink-0">
      <div className="flex items-center gap-2 px-4 border-r border-slate-800">
        <div className="w-6 h-6 bg-blue-600 rounded flex items-center justify-center text-white text-[10px] font-bold">AI</div>
        <span className="text-sm font-semibold text-slate-200">Mission Control</span>
      </div>

      <MetricPill
        label="MTTI avg"
        value={kpis ? '—' : '—'}
        sub="root cause speed"
        highlight
        trend="down"
      />
      <MetricPill
        label="MTTR median"
        value={kpis ? fmtDuration(kpis.mttr_median_ms) : '—'}
        trend="down"
        sub="resolve time"
      />
      <MetricPill
        label="Active"
        value={String(activeCount)}
        sub="investigations"
        highlight={activeCount > 0}
      />
      <MetricPill
        label="RCA rate"
        value={kpis ? `${(kpis.root_cause_found_rate * 100).toFixed(0)}%` : '—'}
        sub="last 7d"
        trend={kpis && kpis.root_cause_found_rate > 0.8 ? 'down' : 'up'}
      />
      {patternAlertCount > 0 && (
        <div className="flex items-center gap-1.5 px-4 text-xs text-yellow-300 bg-yellow-950/20 border-r border-slate-800">
          <AlertTriangle size={12} />
          <span>{patternAlertCount} pattern alert{patternAlertCount > 1 ? 's' : ''}</span>
        </div>
      )}
      <div className="ml-auto px-4 flex items-center gap-1.5 text-xs text-slate-500">
        <Zap size={11} />
        <span>Neo SRE Agent</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Severity + status badges
// ---------------------------------------------------------------------------

const SEV_COLORS: Record<string, string> = {
  critical: 'bg-red-900/50 text-red-400 border border-red-800',
  major:    'bg-orange-900/50 text-orange-400 border border-orange-800',
  warning:  'bg-yellow-900/50 text-yellow-400 border border-yellow-800',
  minor:    'bg-green-900/50 text-green-400 border border-green-800',
  info:     'bg-sky-900/50 text-sky-400 border border-sky-800',
}

const STATUS_COLORS: Record<string, string> = {
  running:           'bg-blue-900/50 text-blue-400',
  awaiting_approval: 'bg-purple-900/50 text-purple-400',
  completed:         'bg-green-900/50 text-green-400',
  failed:            'bg-red-900/50 text-red-400',
  paused:            'bg-yellow-900/50 text-yellow-400',
  pending:           'bg-slate-800 text-slate-400',
}

// ---------------------------------------------------------------------------
// Investigation queue row
// ---------------------------------------------------------------------------

function InvestigationRow({
  inv,
  onClick,
}: {
  inv: IncidentState
  onClick: () => void
}) {
  const isActive = inv.status === 'running' || inv.status === 'awaiting_approval'
  const elapsed = inv.started_at ? elapsedSince(inv.started_at) : null

  return (
    <div
      onClick={onClick}
      className={clsx(
        'group flex items-start gap-3 px-4 py-3 cursor-pointer border-b border-slate-800/60 hover:bg-slate-800/40 transition-colors',
        isActive && 'border-l-2 border-l-blue-600'
      )}
    >
      {/* Severity indicator */}
      <div className={clsx(
        'w-1.5 h-1.5 rounded-full mt-1.5 shrink-0',
        inv.severity === 'critical' ? 'bg-red-500 animate-pulse' :
        inv.severity === 'major'    ? 'bg-orange-500' :
        inv.severity === 'warning'  ? 'bg-yellow-500' :
                                      'bg-slate-500'
      )} />

      {/* Main content */}
      <div className="flex-1 min-w-0 space-y-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-xs text-blue-400">{inv.incident_id}</span>
          <span className={clsx('badge text-[10px]', SEV_COLORS[inv.severity] || 'bg-slate-800 text-slate-400')}>
            {inv.severity}
          </span>
          <span className={clsx('badge text-[10px]', STATUS_COLORS[inv.status] || STATUS_COLORS.pending)}>
            {isActive && <span className="w-1 h-1 bg-blue-400 rounded-full mr-1 animate-pulse inline-block" />}
            {inv.status}
          </span>
        </div>

        <p className="text-sm text-slate-300 truncate" title={inv.summary}>
          {inv.summary || inv.affected_service}
        </p>

        <div className="flex items-center gap-3 text-[10px] text-slate-500">
          {/* Service */}
          {inv.affected_service && (
            <span className="font-mono">{inv.affected_service}</span>
          )}

          {/* MTTI / elapsed time */}
          {isActive && elapsed !== null && (
            <span className="flex items-center gap-1 text-blue-400/70">
              <Clock size={9} />
              investigating: {fmtDuration(elapsed)}
            </span>
          )}
          {!isActive && inv.duration_ms && (
            <span className="flex items-center gap-1">
              <Clock size={9} />
              resolved in {fmtDuration(inv.duration_ms)}
            </span>
          )}

          {/* Confidence */}
          {inv.confidence > 0 && (
            <span className={clsx(
              inv.confidence >= 0.8 ? 'text-green-400' :
              inv.confidence >= 0.6 ? 'text-yellow-400' : 'text-red-400'
            )}>
              {(inv.confidence * 100).toFixed(0)}% conf
            </span>
          )}

          {/* Pattern match indicator */}
          {inv.memory_matches?.length > 0 && (
            <span className="flex items-center gap-1 text-violet-400/70">
              <Zap size={9} />
              {inv.memory_matches.length} pattern{inv.memory_matches.length > 1 ? 's' : ''}
            </span>
          )}
        </div>
      </div>

      {/* Root cause preview */}
      {inv.root_cause && (
        <div className="hidden group-hover:block max-w-48 text-[10px] text-slate-400 border-l border-slate-700 pl-2 leading-tight">
          {inv.root_cause.slice(0, 80)}{inv.root_cause.length > 80 ? '…' : ''}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Inject panel (synthetic incidents for dev)
// ---------------------------------------------------------------------------

function DevInjector({ onInjected }: { onInjected: (id: string) => void }) {
  const [loading, setLoading] = useState<string | null>(null)

  const inject = async (type: string) => {
    setLoading(type)
    try {
      const { devApi } = await import('@/api/client')
      const res = await devApi.injectSynthetic(type)
      onInjected(res.investigation_id)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] text-slate-600 mr-1">inject:</span>
      {['error_spike', 'oomkill', 'latency', 'timeout'].map((t) => (
        <button
          key={t}
          onClick={() => inject(t)}
          disabled={loading === t}
          className="text-[10px] px-2 py-1 rounded bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200 border border-slate-700 disabled:opacity-40 flex items-center gap-1"
        >
          <Plus size={9} />
          {loading === t ? '…' : t}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Queue section header + filter tabs
// ---------------------------------------------------------------------------

type Filter = 'all' | 'active' | 'completed'

function FilterTab({
  label,
  active,
  count,
  onClick,
}: {
  label: string
  active: boolean
  count: number
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'text-xs px-3 py-1 rounded-full border transition-colors',
        active
          ? 'bg-blue-900/40 text-blue-300 border-blue-800'
          : 'text-slate-500 border-slate-800 hover:text-slate-300 hover:border-slate-600'
      )}
    >
      {label}
      {count > 0 && (
        <span className={clsx('ml-1.5', active ? 'text-blue-400' : 'text-slate-600')}>
          {count}
        </span>
      )}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function MissionControl() {
  const navigate = useNavigate()
  const [investigations, setInvestigations] = useState<IncidentState[]>([])
  const [kpis, setKpis] = useState<KPIs | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    try {
      const { investigationsApi } = await import('@/api/client')
      const res = await investigationsApi.list({ limit: 100 })
      setInvestigations(res.investigations)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  const loadKpis = useCallback(async () => {
    try {
      const headers: Record<string, string> = {}
      const token = localStorage.getItem('sentinal_token')
      if (token) headers['Authorization'] = `Bearer ${token}`
      const res = await fetch('/api/v1/metrics/mttr?window_hours=168', { headers })
      if (res.ok) {
        const data = await res.json()
        setKpis(data.kpis ?? null)
      }
    } catch { /* non-critical */ }
  }, [])

  useEffect(() => {
    load()
    loadKpis()
    // Refresh active investigations every 15s
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
  }, [load, loadKpis])

  const filtered = investigations.filter((inv) => {
    if (filter === 'active') return inv.status === 'running' || inv.status === 'awaiting_approval'
    if (filter === 'completed') return inv.status === 'completed' || inv.status === 'failed'
    return true
  })

  const activeCount = investigations.filter(
    (i) => i.status === 'running' || i.status === 'awaiting_approval'
  ).length

  const patternAlertCount = investigations.filter(
    (i) => i.memory_matches?.length > 0 && i.status === 'running'
  ).length

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* ── Metrics strip ── */}
      <MetricsStrip kpis={kpis} activeCount={activeCount} patternAlertCount={patternAlertCount} />

      {/* ── Body ── */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── Left: Investigation queue ── */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden border-r border-slate-800">
          {/* Queue header */}
          <div className="flex items-center gap-3 px-4 py-2.5 border-b border-slate-800 bg-slate-900/60 shrink-0 flex-wrap gap-y-2">
            <div className="flex items-center gap-2">
              <Activity size={13} className="text-slate-500" />
              <span className="text-sm font-medium text-slate-300">Investigations</span>
            </div>
            {/* Filter tabs */}
            <div className="flex items-center gap-1.5">
              <FilterTab
                label="All"
                active={filter === 'all'}
                count={investigations.length}
                onClick={() => setFilter('all')}
              />
              <FilterTab
                label="Active"
                active={filter === 'active'}
                count={activeCount}
                onClick={() => setFilter('active')}
              />
              <FilterTab
                label="Completed"
                active={filter === 'completed'}
                count={investigations.filter(i => i.status === 'completed' || i.status === 'failed').length}
                onClick={() => setFilter('completed')}
              />
            </div>
            <div className="ml-auto flex items-center gap-2">
              <DevInjector onInjected={(id) => navigate(`/investigations/${id}`)} />
              <button
                onClick={() => { setRefreshing(true); load() }}
                className={clsx('text-slate-500 hover:text-slate-300', refreshing && 'animate-spin')}
                title="Refresh"
              >
                <RefreshCw size={13} />
              </button>
            </div>
          </div>

          {/* Queue list */}
          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center h-32 text-slate-600 text-sm">
                Loading investigations…
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-48 gap-3 text-slate-600">
                <Activity size={24} />
                <p className="text-sm">
                  {filter === 'active'
                    ? 'No active investigations'
                    : 'No investigations yet — inject one above'}
                </p>
              </div>
            ) : (
              filtered.map((inv) => (
                <InvestigationRow
                  key={inv.investigation_id}
                  inv={inv}
                  onClick={() => navigate(`/investigations/${inv.investigation_id}`)}
                />
              ))
            )}
          </div>
        </div>

        {/* ── Right: Global pattern intelligence ── */}
        <div className="w-72 shrink-0 flex flex-col overflow-hidden">
          <div className="px-3 py-2.5 border-b border-slate-800 bg-slate-900/60 shrink-0">
            <div className="flex items-center gap-1.5 text-[10px] text-slate-500 uppercase tracking-wider font-medium">
              <Zap size={10} />
              Pattern Intelligence
              {patternAlertCount > 0 && (
                <span className="ml-auto text-yellow-400 flex items-center gap-1">
                  <AlertTriangle size={9} />
                  {patternAlertCount} active
                </span>
              )}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-3">
            {patternAlertCount === 0 && activeCount === 0 ? (
              <div className="text-[11px] text-slate-600 text-center py-8 space-y-2">
                <Zap size={20} className="mx-auto opacity-30" />
                <p>Pattern signals appear here<br />when investigations are active</p>
              </div>
            ) : (
              <PatternIntelligencePanel
                memoryMatches={investigations
                  .filter((i) => i.status === 'running')
                  .flatMap((i) => i.memory_matches ?? [])
                  .slice(0, 10)}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
