// Mission Control — redesigned home page for the SRE agent
// Layout:
//   ┌─────────────────────────────────────────────────────────┐
//   │  Hero stats bar: 5 StatBlocks                           │
//   ├──────────────────┬──────────────────┬───────────────────┤
//   │  Investigations  │  CausalGraph     │  Pattern +        │
//   │  list (40%)      │  (35%)           │  EpisodeMemory    │
//   │                  │                  │  (25%)            │
//   └──────────────────┴──────────────────┴───────────────────┘

import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, Clock, TrendingDown, AlertTriangle, Zap, RefreshCw, Plus, CheckCircle2 } from 'lucide-react'
import { PatternIntelligencePanel } from '@/components/PatternIntelligencePanel'
import { CausalGraph } from '@/components/CausalGraph'
import { EpisodeMemory } from '@/components/EpisodeMemory'
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
// Hero stats bar
// ---------------------------------------------------------------------------

function StatBlock({
  label,
  value,
  sub,
  trend,
  highlight,
  accent,
}: {
  label: string
  value: string
  sub?: string
  trend?: 'up' | 'down' | 'neutral'
  highlight?: boolean
  accent?: string
}) {
  return (
    <div className={clsx(
      'flex flex-col px-5 py-3 border-r border-slate-800 last:border-r-0 min-w-0',
      highlight && 'bg-blue-950/20'
    )}>
      <span className="text-[10px] text-slate-500 uppercase tracking-widest font-medium truncate">{label}</span>
      <div className="flex items-baseline gap-1.5 mt-1">
        <span className={clsx('text-lg font-bold font-mono', accent ?? (highlight ? 'text-blue-300' : 'text-slate-100'))}>
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
        {sub && <span className="text-[10px] text-slate-500 truncate">{sub}</span>}
      </div>
    </div>
  )
}

function HeroStatsBar({ kpis, activeCount, patternAlertCount, investigations }: {
  kpis: KPIs | null
  activeCount: number
  patternAlertCount: number
  investigations: IncidentState[]
}) {
  const completedCount = investigations.filter(i => i.status === 'completed').length
  const meanConf = kpis?.mean_confidence ?? 0

  return (
    <div className="flex items-stretch border-b border-slate-800 bg-slate-900 shrink-0">
      <div className="flex items-center gap-2.5 px-4 py-3 border-r border-slate-800 shrink-0">
        <div className="w-7 h-7 bg-blue-600 rounded-md flex items-center justify-center text-white text-[11px] font-bold">AI</div>
        <div>
          <div className="text-sm font-semibold text-slate-100 leading-tight">Mission Control</div>
          <div className="text-[10px] text-slate-500">Neo SRE Agent</div>
        </div>
      </div>

      <div className="flex flex-1 overflow-x-auto">
        <StatBlock
          label="MTTR median"
          value={kpis ? fmtDuration(kpis.mttr_median_ms) : '—'}
          sub="resolve time"
          trend="down"
          highlight
        />
        <StatBlock
          label="Active"
          value={String(activeCount)}
          sub="investigating"
          accent={activeCount > 0 ? 'text-blue-300' : undefined}
        />
        <StatBlock
          label="Resolved"
          value={String(completedCount)}
          sub="last 7d"
          accent="text-emerald-400"
        />
        <StatBlock
          label="RCA rate"
          value={kpis ? `${(kpis.root_cause_found_rate * 100).toFixed(0)}%` : '—'}
          sub="last 7d"
          trend={kpis && kpis.root_cause_found_rate > 0.8 ? 'down' : 'up'}
        />
        <StatBlock
          label="Mean confidence"
          value={meanConf > 0 ? `${(meanConf * 100).toFixed(0)}%` : '—'}
          sub="AI accuracy"
          accent={meanConf > 0.8 ? 'text-emerald-400' : meanConf > 0.6 ? 'text-yellow-400' : 'text-red-400'}
        />
      </div>

      {patternAlertCount > 0 && (
        <div className="flex items-center gap-1.5 px-4 text-xs text-yellow-300 bg-yellow-950/20 border-l border-slate-800 shrink-0">
          <AlertTriangle size={12} />
          <span>{patternAlertCount} pattern alert{patternAlertCount > 1 ? 's' : ''}</span>
        </div>
      )}
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
// Confidence arc SVG
// ---------------------------------------------------------------------------

function ConfidenceArc({ confidence }: { confidence: number }) {
  if (confidence <= 0) return <div className="w-8 h-8" />
  const r = 12, cx = 16, cy = 16
  const circ = 2 * Math.PI * r
  const arc = (confidence * circ * 0.75)
  const offset = circ * 0.125
  const color = confidence >= 0.8 ? '#10b981' : confidence >= 0.6 ? '#f59e0b' : '#ef4444'
  const pct = Math.round(confidence * 100)

  return (
    <svg width={32} height={32} className="shrink-0">
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="#1e293b" strokeWidth={3}
        strokeDasharray={`${circ * 0.75} ${circ * 0.25}`} strokeDashoffset={offset}
        strokeLinecap="round" transform={`rotate(135 ${cx} ${cy})`} />
      <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth={3}
        strokeDasharray={`${arc} ${circ - arc}`} strokeDashoffset={offset}
        strokeLinecap="round" transform={`rotate(135 ${cx} ${cy})`} />
      <text x={cx} y={cy + 4} textAnchor="middle" fill={color} fontSize={8} fontWeight="700">{pct}</text>
    </svg>
  )
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
        'group flex items-center gap-3 px-3 py-2.5 cursor-pointer border-b border-slate-800/60 hover:bg-slate-800/40 transition-colors',
        isActive && 'border-l-2 border-l-blue-600'
      )}
    >
      {/* Severity dot */}
      <div className={clsx(
        'w-1.5 h-1.5 rounded-full shrink-0',
        inv.severity === 'critical' ? 'bg-red-500 animate-pulse' :
        inv.severity === 'major'    ? 'bg-orange-500' :
        inv.severity === 'warning'  ? 'bg-yellow-500' :
                                      'bg-slate-500'
      )} />

      {/* Confidence arc */}
      <ConfidenceArc confidence={inv.confidence} />

      {/* Main content */}
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-xs text-blue-400 truncate max-w-28">{inv.incident_id}</span>
          <span className={clsx('badge text-[9px]', SEV_COLORS[inv.severity] || 'bg-slate-800 text-slate-400')}>
            {inv.severity}
          </span>
          <span className={clsx('badge text-[9px]', STATUS_COLORS[inv.status] || STATUS_COLORS.pending)}>
            {isActive && <span className="w-1 h-1 bg-blue-400 rounded-full mr-1 animate-pulse inline-block" />}
            {inv.status === 'completed' && <CheckCircle2 size={9} className="mr-1 inline" />}
            {inv.status}
          </span>
        </div>

        <p className="text-xs text-slate-300 truncate" title={inv.summary}>
          {inv.summary || inv.affected_service}
        </p>

        <div className="flex items-center gap-2 text-[10px] text-slate-500">
          {inv.affected_service && (
            <span className="font-mono truncate max-w-24">{inv.affected_service}</span>
          )}
          {isActive && elapsed !== null && (
            <span className="flex items-center gap-0.5 text-blue-400/70">
              <Clock size={8} />
              {fmtDuration(elapsed)}
            </span>
          )}
          {!isActive && inv.duration_ms && (
            <span className="flex items-center gap-0.5">
              <Clock size={8} />
              {fmtDuration(inv.duration_ms)}
            </span>
          )}
          {inv.memory_matches?.length > 0 && (
            <span className="flex items-center gap-0.5 text-violet-400/70">
              <Zap size={8} />
              {inv.memory_matches.length}
            </span>
          )}
        </div>
      </div>
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
    <div className="flex items-center gap-1 flex-wrap">
      <span className="text-[10px] text-slate-600">inject:</span>
      {['error_spike', 'oomkill', 'latency', 'timeout'].map((t) => (
        <button
          key={t}
          onClick={() => inject(t)}
          disabled={loading === t}
          className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-slate-200 border border-slate-700 disabled:opacity-40 flex items-center gap-0.5"
        >
          <Plus size={8} />
          {loading === t ? '…' : t}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Queue filter tabs
// ---------------------------------------------------------------------------

type Filter = 'all' | 'active' | 'completed'

function FilterTab({ label, active, count, onClick }: { label: string; active: boolean; count: number; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={clsx(
        'text-[11px] px-2.5 py-1 rounded-full border transition-colors',
        active
          ? 'bg-blue-900/40 text-blue-300 border-blue-800'
          : 'text-slate-500 border-slate-800 hover:text-slate-300 hover:border-slate-600'
      )}
    >
      {label}
      {count > 0 && (
        <span className={clsx('ml-1', active ? 'text-blue-400' : 'text-slate-600')}>{count}</span>
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

  // Pick the most active/recent investigation for Episode Memory context
  const topActiveService = investigations
    .filter(i => i.status === 'running' && i.affected_service)
    .map(i => i.affected_service)[0]

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* ── Hero stats bar ── */}
      <HeroStatsBar kpis={kpis} activeCount={activeCount} patternAlertCount={patternAlertCount} investigations={investigations} />

      {/* ── 3-panel body ── */}
      <div className="flex flex-1 overflow-hidden min-h-0">

        {/* ── Panel 1: Investigation queue (40%) ── */}
        <div className="flex flex-col min-w-0 overflow-hidden border-r border-slate-800" style={{ width: '40%' }}>
          {/* Queue header */}
          <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0 flex-wrap gap-y-1.5">
            <div className="flex items-center gap-1.5">
              <Activity size={12} className="text-slate-500" />
              <span className="text-xs font-medium text-slate-300">Investigations</span>
            </div>
            <div className="flex items-center gap-1">
              <FilterTab label="All" active={filter === 'all'} count={investigations.length} onClick={() => setFilter('all')} />
              <FilterTab label="Active" active={filter === 'active'} count={activeCount} onClick={() => setFilter('active')} />
              <FilterTab label="Done" active={filter === 'completed'} count={investigations.filter(i => i.status === 'completed' || i.status === 'failed').length} onClick={() => setFilter('completed')} />
            </div>
            <div className="ml-auto flex items-center gap-1.5">
              <DevInjector onInjected={(id) => navigate(`/investigations/${id}`)} />
              <button
                onClick={() => { setRefreshing(true); load() }}
                className={clsx('text-slate-500 hover:text-slate-300', refreshing && 'animate-spin')}
                title="Refresh"
              >
                <RefreshCw size={12} />
              </button>
            </div>
          </div>

          {/* Queue list */}
          <div className="flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center h-32 text-slate-600 text-xs">
                Loading investigations…
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-48 gap-2 text-slate-600">
                <Activity size={20} />
                <p className="text-xs text-center">
                  {filter === 'active' ? 'No active investigations' : 'No investigations yet — inject one above'}
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

        {/* ── Panel 2: CausalGraph (35%) ── */}
        <div className="flex flex-col min-w-0 overflow-hidden border-r border-slate-800" style={{ width: '35%' }}>
          <div className="px-3 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0 flex items-center gap-1.5">
            <span className="text-[10px] text-slate-500 uppercase tracking-widest font-medium">Service Topology</span>
            <span className="text-[10px] text-slate-600 ml-1">— click node for details</span>
          </div>
          <div className="flex-1 overflow-hidden">
            <CausalGraph />
          </div>
        </div>

        {/* ── Panel 3: Pattern Intel + Episode Memory (25%) ── */}
        <div className="flex flex-col overflow-hidden" style={{ width: '25%' }}>
          {/* Pattern intel section */}
          <div className="flex flex-col" style={{ flex: '1 1 50%', minHeight: 0, overflow: 'hidden' }}>
            <div className="px-3 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0">
              <div className="flex items-center gap-1.5 text-[10px] text-slate-500 uppercase tracking-widest font-medium">
                <Zap size={10} />
                Pattern Intelligence
                {patternAlertCount > 0 && (
                  <span className="ml-auto text-yellow-400 flex items-center gap-1">
                    <AlertTriangle size={9} />
                    {patternAlertCount}
                  </span>
                )}
              </div>
            </div>
            <div className="flex-1 overflow-y-auto p-3">
              {patternAlertCount === 0 && activeCount === 0 ? (
                <div className="text-[11px] text-slate-600 text-center py-6 space-y-1.5">
                  <Zap size={18} className="mx-auto opacity-30" />
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

          {/* Episode memory section */}
          <div className="flex flex-col border-t border-slate-800" style={{ flex: '1 1 50%', minHeight: 0, overflow: 'hidden' }}>
            <div className="px-3 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0">
              <span className="text-[10px] text-slate-500 uppercase tracking-widest font-medium">Episode Memory</span>
            </div>
            <div className="flex-1 overflow-y-auto p-3">
              <EpisodeMemory service={topActiveService} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
