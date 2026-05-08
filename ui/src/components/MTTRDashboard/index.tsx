// MTTR Dashboard — agent performance and ROI telemetry
// Answers the exec question: "Is this thing actually faster than a human?"

import React, { useEffect, useState, useCallback } from 'react'
import {
  AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, LineChart, Line,
} from 'recharts'
import {
  Clock, TrendingDown, CheckCircle2, Zap,
  AlertCircle, BarChart2, RefreshCw, ChevronUp, ChevronDown,
  Brain, Flame, Activity,
} from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface KPIs {
  mttr_median_ms: number
  mttr_p95_ms: number
  mttr_p99_ms: number
  total_investigations: number
  last_24h_count: number
  last_7d_count: number
  root_cause_found_rate: number
  mean_confidence: number
  fix_proposed_rate: number
  fix_applied_rate: number
}

interface TrendPoint {
  date: string
  count: number
  mttr_median_ms: number
  root_cause_found: number
  mean_confidence: number
}

interface ServiceRow {
  service: string
  count: number
  mttr_median_ms: number
  mttr_p95_ms: number
  root_cause_found_rate: number
  mean_confidence: number
  fix_proposed_rate: number
}

interface ROI {
  window_hours: number
  investigations: number
  agent_median_mttr_min: number
  human_baseline_min: number
  time_saved_per_incident_min: number
  total_time_saved_hours: number
  deflection_count: number
  deflection_rate: number
}

interface CalibrationBucket {
  bucket_min: number
  bucket_max: number
  predicted_mean: number
  actual_correct_rate: number
  count: number
}

interface DashboardData {
  window_hours: number
  kpis: KPIs
  trend: TrendPoint[]
  service_breakdown: ServiceRow[]
  roi: ROI
  calibration: CalibrationBucket[]
}

interface SLOHealth {
  total_slos: number
  by_status: Record<string, number>
  burning_or_worse: number
}

interface PILMetrics {
  accuracy: Record<string, { precision: number; tp: number; fp: number }>
  active_predictions: { total: number; by_severity: Record<string, number> }
  slo_health: SLOHealth
  runner_iteration: number
}

// Intelligence surface decision-metrics shapes
interface PatternEntry {
  pattern_type: string
  active_count: number
  avg_confidence: number
  warming: boolean
  precision: number
  reinforcement_count: number
}
interface DecisionMetrics {
  pattern_intelligence: { patterns: PatternEntry[]; total_active: number; runner_ready: boolean }
  feedback_quality: { total_investigations: number; warming: boolean; root_cause_found_rate: number; low_confidence_rate: number; mean_confidence: number; citation_coverage: number; unresolved_rca_count: number }
  convergence: { warming: boolean; calibration_ece: number; calibration_samples: number; mean_confidence: number; mean_source_count: number; hypothesis_diversity: number }
  adaptive_intelligence: { warming: boolean; learning_mode: string; rolling_quality: number | null; deterministic_influence_pct: number; adaptive_influence_pct: number; total_evolved_steps: number }
  evidence_diversity: { warming: boolean; total_analyzed: number; multi_source_pct: number; single_source_warning_count: number }
  source_weights: { warming: boolean; rows: { incident_type: string; step: string; weight: number; delta: number; calls: number; stable: boolean; neutral: boolean }[]; total_evolved: number }
  learning_safety: { threshold_status: string; drifted_thresholds: number; circuit_breaker_fired: boolean; calibration_stale: boolean; experience_count: number; experience_warming: boolean; no_behavioral_drift: boolean; recommendations: string[] }
  mttr_impact: { investigation_count: number; rca_confidence_quality: number; evidence_usefulness: number; fix_proposed_rate: number; warming: boolean }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function msToHuman(ms: number): string {
  if (ms === 0) return '—'
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(0)}s`
  const m = s / 60
  if (m < 60) return `${m.toFixed(1)}m`
  return `${(m / 60).toFixed(1)}h`
}

function pct(v: number) {
  return `${(v * 100).toFixed(1)}%`
}

// ---------------------------------------------------------------------------
// KPI Card
// ---------------------------------------------------------------------------

function KPICard({
  label, value, sub, icon, color = 'blue', delta,
}: {
  label: string
  value: string
  sub?: string
  icon: React.ReactNode
  color?: 'blue' | 'green' | 'orange' | 'purple'
  delta?: { value: string; positive: boolean }
}) {
  const colors = {
    blue:   { bg: 'bg-blue-900/20',   border: 'border-blue-800/40',   icon: 'text-blue-400'   },
    green:  { bg: 'bg-green-900/20',  border: 'border-green-800/40',  icon: 'text-green-400'  },
    orange: { bg: 'bg-orange-900/20', border: 'border-orange-800/40', icon: 'text-orange-400' },
    purple: { bg: 'bg-purple-900/20', border: 'border-purple-800/40', icon: 'text-purple-400' },
  }[color]

  return (
    <div className={clsx('rounded-xl border p-4 space-y-2', colors.bg, colors.border)}>
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-400 uppercase tracking-wider">{label}</span>
        <span className={clsx('opacity-70', colors.icon)}>{icon}</span>
      </div>
      <div className="text-2xl font-bold text-slate-100 tabular-nums">{value}</div>
      <div className="flex items-center gap-2">
        {sub && <span className="text-xs text-slate-500">{sub}</span>}
        {delta && (
          <span className={clsx(
            'flex items-center gap-0.5 text-xs font-medium',
            delta.positive ? 'text-green-400' : 'text-red-400'
          )}>
            {delta.positive ? <ChevronDown size={12} /> : <ChevronUp size={12} />}
            {delta.value}
          </span>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// ROI Banner
// ---------------------------------------------------------------------------

function ROIBanner({ roi }: { roi: ROI }) {
  if (!roi.investigations) return null

  return (
    <div className="rounded-xl border border-green-800/40 bg-green-900/10 p-5">
      <div className="flex items-center gap-2 mb-4">
        <Zap size={16} className="text-green-400" />
        <span className="text-sm font-semibold text-green-300">Agent ROI — Last {roi.window_hours / 24} days</span>
        <span className="text-xs text-slate-500 ml-auto">
          vs {roi.human_baseline_min}min human baseline
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="text-center">
          <div className="text-3xl font-bold text-green-400 tabular-nums">
            {roi.total_time_saved_hours}h
          </div>
          <div className="text-xs text-slate-400 mt-1">Total time saved</div>
        </div>
        <div className="text-center">
          <div className="text-3xl font-bold text-green-400 tabular-nums">
            {roi.time_saved_per_incident_min}m
          </div>
          <div className="text-xs text-slate-400 mt-1">Saved per incident</div>
        </div>
        <div className="text-center">
          <div className="text-3xl font-bold text-blue-400 tabular-nums">
            {roi.deflection_count}
          </div>
          <div className="text-xs text-slate-400 mt-1">
            Autonomous resolutions ({pct(roi.deflection_rate)})
          </div>
        </div>
        <div className="text-center">
          <div className="text-3xl font-bold text-slate-200 tabular-nums">
            {roi.agent_median_mttr_min}m
          </div>
          <div className="text-xs text-slate-400 mt-1">
            Agent median vs {roi.human_baseline_min}m human
          </div>
        </div>
      </div>

      {/* Speed bar */}
      <div className="mt-4 space-y-1">
        <div className="flex justify-between text-xs text-slate-500">
          <span>Agent MTTR</span>
          <span>Human baseline</span>
        </div>
        <div className="relative h-2 bg-slate-800 rounded-full overflow-hidden">
          <div
            className="absolute left-0 h-full bg-green-500 rounded-full transition-all duration-700"
            style={{ width: `${Math.min(100, (roi.agent_median_mttr_min / roi.human_baseline_min) * 100)}%` }}
          />
          <div
            className="absolute h-full w-0.5 bg-slate-400"
            style={{ left: '100%' }}
          />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// MTTR Trend Chart
// ---------------------------------------------------------------------------

const CustomTooltip = ({ active, payload, label }: any) => {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-slate-900 border border-slate-700 rounded-lg p-3 text-xs space-y-1">
      <div className="text-slate-400 font-medium">{label}</div>
      {payload.map((p: any) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full" style={{ background: p.color }} />
          <span className="text-slate-400">{p.name}:</span>
          <span className="text-slate-200 font-mono">
            {p.name === 'MTTR (min)' ? `${p.value.toFixed(1)}m` : p.value}
          </span>
        </div>
      ))}
    </div>
  )
}

function TrendChart({ data }: { data: TrendPoint[] }) {
  const chartData = data.map(d => ({
    date: d.date.slice(5),     // "MM-DD"
    'MTTR (min)': d.mttr_median_ms > 0 ? parseFloat((d.mttr_median_ms / 60_000).toFixed(2)) : null,
    'Incidents': d.count,
    'Root cause %': d.count > 0 ? Math.round(d.root_cause_found / d.count * 100) : null,
  }))

  return (
    <div className="space-y-1">
      <div className="text-xs text-slate-400 mb-3">30-day MTTR trend (minutes)</div>
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <defs>
            <linearGradient id="mttrGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#3b82f6" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
          <YAxis tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} axisLine={false} />
          <Tooltip content={<CustomTooltip />} />
          <Area
            type="monotone"
            dataKey="MTTR (min)"
            stroke="#3b82f6"
            fill="url(#mttrGrad)"
            strokeWidth={2}
            dot={false}
            connectNulls
          />
        </AreaChart>
      </ResponsiveContainer>

      <div className="text-xs text-slate-400 mb-2 mt-4">Daily incident volume</div>
      <ResponsiveContainer width="100%" height={100}>
        <BarChart data={chartData} margin={{ top: 0, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
          <XAxis dataKey="date" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
          <YAxis tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} axisLine={false} />
          <Tooltip content={<CustomTooltip />} />
          <Bar dataKey="Incidents" fill="#6366f1" radius={[2, 2, 0, 0]} maxBarSize={20} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Calibration Chart
// ---------------------------------------------------------------------------

function CalibrationChart({ data }: { data: CalibrationBucket[] }) {
  if (!data.length) return (
    <div className="h-40 flex items-center justify-center text-slate-500 text-sm">
      Insufficient data — run more investigations
    </div>
  )

  const chartData = data.map(b => ({
    bucket: `${b.bucket_min.toFixed(0)}-${b.bucket_max.toFixed(0)}%`,
    predicted: parseFloat(b.predicted_mean.toFixed(1)),
    actual: parseFloat((b.actual_correct_rate * 100).toFixed(1)),
    n: b.count,
  }))

  // Perfect calibration reference line data
  return (
    <div>
      <div className="text-xs text-slate-400 mb-3">
        Confidence calibration — predicted vs actual correct rate
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <LineChart data={chartData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
          <XAxis dataKey="bucket" tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} />
          <YAxis tick={{ fill: '#64748b', fontSize: 10 }} tickLine={false} axisLine={false} domain={[0, 100]} />
          <Tooltip content={<CustomTooltip />} />
          <Line type="monotone" dataKey="predicted" stroke="#6366f1" strokeWidth={2} dot={false} name="Predicted %" />
          <Line type="monotone" dataKey="actual" stroke="#10b981" strokeWidth={2} dot={{ r: 3 }} name="Actual %" />
        </LineChart>
      </ResponsiveContainer>
      <div className="flex items-center gap-4 mt-2 text-xs text-slate-500">
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-indigo-400 inline-block" /> Predicted confidence</span>
        <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-emerald-400 inline-block" /> Actual correct rate</span>
        <span className="ml-auto italic">Lines should align for perfect calibration</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Service Breakdown Table
// ---------------------------------------------------------------------------

type SortKey = keyof ServiceRow
type SortDir = 'asc' | 'desc'

function ServiceTable({ rows }: { rows: ServiceRow[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('count')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const sorted = [...rows].sort((a, b) => {
    const av = a[sortKey]
    const bv = b[sortKey]
    const cmp = typeof av === 'string' ? av.localeCompare(bv as string) : (av as number) - (bv as number)
    return sortDir === 'desc' ? -cmp : cmp
  })

  const toggleSort = (key: SortKey) => {
    if (key === sortKey) setSortDir(d => d === 'desc' ? 'asc' : 'desc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const SortIcon = ({ k }: { k: SortKey }) =>
    sortKey === k
      ? (sortDir === 'desc' ? <ChevronDown size={11} className="inline" /> : <ChevronUp size={11} className="inline" />)
      : null

  const th = (label: string, key: SortKey, align = 'left') => (
    <th
      className={clsx(
        'px-3 py-2 text-xs text-slate-400 font-medium cursor-pointer select-none whitespace-nowrap hover:text-slate-200',
        align === 'right' ? 'text-right' : 'text-left'
      )}
      onClick={() => toggleSort(key)}
    >
      {label} <SortIcon k={key} />
    </th>
  )

  if (!rows.length) return (
    <div className="py-10 text-center text-slate-500 text-sm">No data yet</div>
  )

  const maxMttr = Math.max(...rows.map(r => r.mttr_median_ms), 1)

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800">
            {th('Service', 'service')}
            {th('Incidents', 'count', 'right')}
            {th('Median MTTR', 'mttr_median_ms', 'right')}
            {th('P95 MTTR', 'mttr_p95_ms', 'right')}
            {th('RCA Rate', 'root_cause_found_rate', 'right')}
            {th('Confidence', 'mean_confidence', 'right')}
            {th('Fix Rate', 'fix_proposed_rate', 'right')}
          </tr>
        </thead>
        <tbody>
          {sorted.map(row => {
            const mttrPct = (row.mttr_median_ms / maxMttr) * 100
            const rcColor = row.root_cause_found_rate >= 0.8
              ? 'text-green-400' : row.root_cause_found_rate >= 0.6
              ? 'text-yellow-400' : 'text-red-400'

            return (
              <tr key={row.service} className="border-b border-slate-800/40 hover:bg-slate-800/30">
                <td className="px-3 py-2.5 font-mono text-slate-200 text-xs">{row.service}</td>
                <td className="px-3 py-2.5 text-right tabular-nums text-slate-300">{row.count}</td>
                <td className="px-3 py-2.5 text-right">
                  <div className="flex items-center justify-end gap-2">
                    <div className="w-16 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-blue-500 rounded-full"
                        style={{ width: `${mttrPct}%` }}
                      />
                    </div>
                    <span className="tabular-nums text-slate-300 text-xs w-12 text-right">
                      {msToHuman(row.mttr_median_ms)}
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums text-slate-400 text-xs">
                  {msToHuman(row.mttr_p95_ms)}
                </td>
                <td className={clsx('px-3 py-2.5 text-right tabular-nums text-xs font-medium', rcColor)}>
                  {pct(row.root_cause_found_rate)}
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums text-slate-300 text-xs">
                  {row.mean_confidence.toFixed(0)}%
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums text-slate-400 text-xs">
                  {pct(row.fix_proposed_rate)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Pattern Intelligence Panel
// ---------------------------------------------------------------------------

function PILPanel({ pil }: { pil: PILMetrics }) {
  const severityColors: Record<string, string> = {
    IMMINENT: 'text-red-400 bg-red-900/30 border-red-800/40',
    LIKELY:   'text-yellow-400 bg-yellow-900/30 border-yellow-800/40',
    WATCH:    'text-blue-400 bg-blue-900/30 border-blue-800/40',
  }

  const sloStatusColors: Record<string, string> = {
    BREACHED:  'text-red-400',
    CRITICAL:  'text-orange-400',
    BURNING:   'text-yellow-400',
    WATCHING:  'text-blue-400',
    OK:        'text-green-400',
  }

  const patternLabels: Record<string, string> = {
    trend_drift:   'Trend Drift',
    rate_accel:    'Rate Accel',
    cross_service: 'Cross-Service',
    post_deploy:   'Post-Deploy',
    slo_burn:      'SLO Burn',
  }

  return (
    <div className="bg-slate-900 rounded-xl border border-slate-800 p-5 space-y-5">
      <div className="flex items-center gap-2">
        <Brain size={15} className="text-indigo-400" />
        <h2 className="text-sm font-semibold text-slate-200">Pattern Intelligence Layer</h2>
        <span className="ml-auto text-xs text-slate-500">cycle #{pil.runner_iteration}</span>
      </div>

      {/* Active predictions by severity */}
      <div>
        <div className="text-xs text-slate-400 mb-2 uppercase tracking-wider">Active Predictions</div>
        <div className="flex gap-2 flex-wrap">
          {pil.active_predictions.total === 0 ? (
            <span className="text-xs text-slate-500 italic">No active predictions</span>
          ) : (
            Object.entries(pil.active_predictions.by_severity).map(([sev, count]) => (
              <span key={sev} className={clsx(
                'px-2.5 py-1 rounded-full text-xs font-medium border',
                severityColors[sev] ?? 'text-slate-400 bg-slate-800 border-slate-700'
              )}>
                {sev} · {count}
              </span>
            ))
          )}
          <span className="px-2.5 py-1 rounded-full text-xs text-slate-400 bg-slate-800 border border-slate-700">
            Total: {pil.active_predictions.total}
          </span>
        </div>
      </div>

      {/* Detector accuracy table */}
      {Object.keys(pil.accuracy).length > 0 && (
        <div>
          <div className="text-xs text-slate-400 mb-2 uppercase tracking-wider">Detector Precision</div>
          <div className="space-y-1.5">
            {Object.entries(pil.accuracy).map(([pattern, stats]) => (
              <div key={pattern} className="flex items-center gap-3 text-xs">
                <span className="w-28 text-slate-400 shrink-0">{patternLabels[pattern] ?? pattern}</span>
                <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-indigo-500 rounded-full"
                    style={{ width: `${(stats.precision * 100).toFixed(0)}%` }}
                  />
                </div>
                <span className="w-10 text-right tabular-nums text-slate-300">
                  {(stats.precision * 100).toFixed(0)}%
                </span>
                <span className="text-slate-600">
                  {stats.tp}TP / {stats.fp}FP
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* SLO health summary */}
      <div>
        <div className="flex items-center gap-2 mb-2">
          <Flame size={13} className={pil.slo_health.burning_or_worse > 0 ? 'text-orange-400' : 'text-slate-600'} />
          <div className="text-xs text-slate-400 uppercase tracking-wider">SLO Health</div>
          {pil.slo_health.burning_or_worse > 0 && (
            <span className="text-xs text-orange-400 font-medium">
              {pil.slo_health.burning_or_worse} burning or worse
            </span>
          )}
          <span className="ml-auto text-xs text-slate-500">{pil.slo_health.total_slos} SLOs tracked</span>
        </div>
        <div className="flex gap-2 flex-wrap">
          {Object.entries(pil.slo_health.by_status).map(([status, count]) => (
            <span key={status} className={clsx('text-xs font-medium tabular-nums', sloStatusColors[status] ?? 'text-slate-400')}>
              {status}: {count}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Intelligence Surface Widgets
// ---------------------------------------------------------------------------

function WarmingBadge({ label = 'Evidence warming' }: { label?: string }) {
  return (
    <span className="text-xs text-slate-500 italic flex items-center gap-1">
      <span className="w-1.5 h-1.5 rounded-full bg-slate-600 animate-pulse inline-block" />
      {label}
    </span>
  )
}

function IntelRow({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent?: string }) {
  return (
    <div className="flex items-center justify-between text-xs py-1">
      <span className="text-slate-400">{label}</span>
      <div className="text-right">
        <span className={accent ?? 'text-slate-200'}>{value}</span>
        {sub && <span className="text-slate-600 ml-1.5">{sub}</span>}
      </div>
    </div>
  )
}

function IntelSection({ title, icon, children, borderColor = 'border-slate-800' }: {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
  borderColor?: string
}) {
  const [open, setOpen] = useState(true)
  return (
    <div className={clsx('bg-slate-900 rounded-xl border p-4', borderColor)}>
      <button
        className="w-full flex items-center gap-2 mb-3"
        onClick={() => setOpen(v => !v)}
      >
        <span className="text-slate-500">{icon}</span>
        <span className="text-xs font-semibold text-slate-300 uppercase tracking-wider flex-1 text-left">{title}</span>
        {open ? <ChevronUp size={12} className="text-slate-600" /> : <ChevronDown size={12} className="text-slate-600" />}
      </button>
      {open && children}
    </div>
  )
}

function PatternIntelWidget({ dm }: { dm: DecisionMetrics }) {
  const pi = dm.pattern_intelligence
  if (!pi.runner_ready) return (
    <IntelSection title="Pattern Intelligence" icon={<Brain size={13} />}>
      <WarmingBadge label="Runner initialising" />
    </IntelSection>
  )
  const patternLabels: Record<string, string> = {
    trend_drift: 'Trend Drift', rate_accel: 'Rate Accel',
    cross_service: 'Cross-Service', post_deploy: 'Post-Deploy', slo_burn: 'SLO Burn',
  }
  return (
    <IntelSection title="Pattern Intelligence" icon={<Brain size={13} />}>
      {pi.patterns.length === 0 ? (
        <WarmingBadge label="No active patterns" />
      ) : (
        <div className="space-y-2">
          {pi.patterns.map(p => (
            <div key={p.pattern_type} className="flex items-center gap-3">
              <span className="w-28 text-xs text-slate-400 shrink-0">{patternLabels[p.pattern_type] ?? p.pattern_type}</span>
              <div className="flex-1 h-1 bg-slate-800 rounded-full overflow-hidden">
                <div className="h-full bg-indigo-500 rounded-full" style={{ width: `${(p.avg_confidence * 100).toFixed(0)}%` }} />
              </div>
              <span className="text-xs text-slate-300 tabular-nums w-8 text-right">{(p.avg_confidence * 100).toFixed(0)}%</span>
              <span className="text-xs text-slate-600 w-16 text-right">{p.reinforcement_count} reinforced</span>
              {p.warming && <WarmingBadge label="warming" />}
            </div>
          ))}
        </div>
      )}
    </IntelSection>
  )
}

function FeedbackQualityWidget({ dm }: { dm: DecisionMetrics }) {
  const fq = dm.feedback_quality
  return (
    <IntelSection title="Feedback & Quality" icon={<Activity size={13} />}>
      {fq.warming ? <WarmingBadge /> : (
        <div className="divide-y divide-slate-800/50">
          <IntelRow label="Investigations analyzed" value={String(fq.total_investigations)} />
          <IntelRow label="RCA found rate" value={`${(fq.root_cause_found_rate * 100).toFixed(1)}%`}
            accent={fq.root_cause_found_rate >= 0.8 ? 'text-green-400' : fq.root_cause_found_rate >= 0.6 ? 'text-yellow-400' : 'text-red-400'} />
          <IntelRow label="Low-confidence rate" value={`${(fq.low_confidence_rate * 100).toFixed(1)}%`}
            accent={fq.low_confidence_rate < 0.1 ? 'text-green-400' : 'text-yellow-400'} />
          <IntelRow label="Unresolved RCAs" value={String(fq.unresolved_rca_count)} />
          <IntelRow label="Evidence usefulness" value={`${(fq.citation_coverage * 100).toFixed(0)}%`} sub="citation coverage" />
        </div>
      )}
    </IntelSection>
  )
}

function ConvergenceWidget({ dm }: { dm: DecisionMetrics }) {
  const cv = dm.convergence
  return (
    <IntelSection title="Intelligence Convergence" icon={<TrendingDown size={13} />}>
      {cv.warming ? <WarmingBadge label="Calibration warming" /> : (
        <div className="divide-y divide-slate-800/50">
          <IntelRow label="Calibration ECE" value={cv.calibration_ece.toFixed(3)}
            accent={cv.calibration_ece < 0.05 ? 'text-green-400' : cv.calibration_ece < 0.15 ? 'text-yellow-400' : 'text-red-400'}
            sub={`${cv.calibration_samples} samples`} />
          <IntelRow label="Mean confidence" value={`${cv.mean_confidence.toFixed(0)}%`} />
          <IntelRow label="Avg sources/investigation" value={cv.mean_source_count.toFixed(1)} />
        </div>
      )}
    </IntelSection>
  )
}

function AdaptiveIntelWidget({ dm }: { dm: DecisionMetrics }) {
  const ai = dm.adaptive_intelligence
  const modeColor = ai.learning_mode === 'healthy' ? 'text-green-400'
    : ai.learning_mode === 'degraded' ? 'text-red-400' : 'text-slate-500'
  const modeLabel = ai.learning_mode === 'healthy' ? 'Learning active'
    : ai.learning_mode === 'degraded' ? 'Circuit breaker fired'
    : 'Adaptive scoring observing'
  return (
    <IntelSection title="Adaptive Intelligence" icon={<Zap size={13} />}>
      {ai.warming ? <WarmingBadge label="Neutral weights" /> : (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className={clsx('text-xs font-medium', modeColor)}>{modeLabel}</span>
            {ai.rolling_quality !== null && (
              <span className="text-xs text-slate-500 ml-auto">rolling avg {(ai.rolling_quality * 100).toFixed(0)}%</span>
            )}
          </div>
          <div className="divide-y divide-slate-800/50">
            <IntelRow label="Deterministic influence" value={`${(ai.deterministic_influence_pct * 100).toFixed(0)}%`} />
            <IntelRow label="Adaptive influence" value={`${(ai.adaptive_influence_pct * 100).toFixed(0)}%`}
              accent={ai.adaptive_influence_pct > 0.1 ? 'text-violet-400' : 'text-slate-200'} />
            <IntelRow label="Evolved steps tracked" value={String(ai.total_evolved_steps)} />
          </div>
        </div>
      )}
    </IntelSection>
  )
}

function EvidenceDiversityWidget({ dm }: { dm: DecisionMetrics }) {
  const ev = dm.evidence_diversity
  return (
    <IntelSection title="Evidence Diversity" icon={<BarChart2 size={13} />}>
      {ev.warming ? <WarmingBadge /> : (
        <div className="space-y-2">
          <div className="flex items-center gap-2 mb-1">
            <div className="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
              <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${(ev.multi_source_pct * 100).toFixed(0)}%` }} />
            </div>
            <span className="text-xs text-slate-300 tabular-nums w-10 text-right">
              {(ev.multi_source_pct * 100).toFixed(0)}%
            </span>
          </div>
          <div className="divide-y divide-slate-800/50">
            <IntelRow label="Multi-source corroboration" value={`${(ev.multi_source_pct * 100).toFixed(0)}%`}
              accent={ev.multi_source_pct >= 0.7 ? 'text-green-400' : 'text-yellow-400'} />
            <IntelRow label="Single-source RCAs" value={String(ev.single_source_warning_count)}
              accent={ev.single_source_warning_count > 0 ? 'text-yellow-400' : 'text-green-400'} />
            <IntelRow label="Investigations analyzed" value={String(ev.total_analyzed)} />
          </div>
          {ev.single_source_warning_count > 0 && (
            <p className="text-xs text-yellow-600 italic mt-1">
              {ev.single_source_warning_count} investigations relied on a single evidence source
            </p>
          )}
        </div>
      )}
    </IntelSection>
  )
}

function SourceWeightWidget({ dm }: { dm: DecisionMetrics }) {
  const sw = dm.source_weights
  const topRows = sw.rows.slice(0, 8)
  return (
    <IntelSection title="Source Weight Evolution" icon={<Activity size={13} />} borderColor="border-slate-800">
      {sw.warming ? <WarmingBadge label="Neutral weights — no data yet" /> : (
        <div className="space-y-2">
          <div className="text-xs text-slate-500 mb-2">
            {sw.total_evolved} steps diverged from neutral baseline (1.0)
          </div>
          <div className="space-y-1">
            {topRows.map((r, i) => {
              const bar = Math.min(100, Math.abs(r.delta) / 0.5 * 100)
              const color = r.delta > 0 ? 'bg-green-500' : 'bg-red-500'
              return (
                <div key={i} className="flex items-center gap-2 text-xs">
                  <span className="w-28 text-slate-500 truncate">{r.step}</span>
                  <div className="w-20 h-1 bg-slate-800 rounded-full overflow-hidden">
                    <div className={`h-full rounded-full ${color}`} style={{ width: `${bar}%` }} />
                  </div>
                  <span className={clsx('tabular-nums w-12 text-right', r.delta > 0 ? 'text-green-400' : 'text-red-400')}>
                    {r.delta > 0 ? '+' : ''}{r.delta.toFixed(2)}
                  </span>
                  <span className="text-slate-600 w-8 text-right">{r.calls}</span>
                  {!r.stable && <span className="text-slate-600 italic">warming</span>}
                </div>
              )
            })}
          </div>
          {sw.rows.length > 8 && (
            <p className="text-xs text-slate-600">+{sw.rows.length - 8} more steps</p>
          )}
        </div>
      )}
    </IntelSection>
  )
}

function LearningSafetyWidget({ dm }: { dm: DecisionMetrics }) {
  const ls = dm.learning_safety
  const statusColor = ls.threshold_status === 'OK' ? 'text-green-400'
    : ls.threshold_status === 'WARNING' ? 'text-yellow-400' : 'text-red-400'
  return (
    <IntelSection title="Learning Safety" icon={<CheckCircle2 size={13} />}
      borderColor={ls.circuit_breaker_fired || ls.drifted_thresholds > 0 ? 'border-yellow-800/40' : 'border-slate-800'}>
      <div className="space-y-2">
        <div className="flex flex-wrap gap-2 mb-2">
          <span className={clsx('text-xs px-2 py-0.5 rounded-full border',
            ls.no_behavioral_drift ? 'text-green-400 border-green-800/40 bg-green-900/20' : 'text-yellow-400 border-yellow-800/40 bg-yellow-900/20')}>
            {ls.no_behavioral_drift ? 'No drift detected' : `${ls.drifted_thresholds} threshold(s) drifted`}
          </span>
          <span className={clsx('text-xs px-2 py-0.5 rounded-full border',
            !ls.circuit_breaker_fired ? 'text-green-400 border-green-800/40 bg-green-900/20' : 'text-red-400 border-red-800/40 bg-red-900/20')}>
            {ls.circuit_breaker_fired ? 'Circuit breaker fired' : 'Reinforcement active'}
          </span>
          {ls.experience_warming && (
            <span className="text-xs px-2 py-0.5 rounded-full border text-slate-500 border-slate-700 bg-slate-800/50">
              Experience warming
            </span>
          )}
        </div>
        <div className="divide-y divide-slate-800/50">
          <IntelRow label="Threshold status" value={ls.threshold_status} accent={statusColor} />
          <IntelRow label="Drifted thresholds" value={String(ls.drifted_thresholds)} />
          <IntelRow label="Calibration stale" value={ls.calibration_stale ? 'Yes' : 'No'}
            accent={ls.calibration_stale ? 'text-yellow-400' : 'text-green-400'} />
          <IntelRow label="Stored experiences" value={String(ls.experience_count)} />
        </div>
        {ls.recommendations?.slice(0, 2).map((r, i) => (
          <p key={i} className="text-xs text-slate-500 italic">{r}</p>
        ))}
      </div>
    </IntelSection>
  )
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-64 text-center space-y-3">
      <BarChart2 size={40} className="text-slate-700" />
      <div className="text-slate-400 font-medium">No investigations recorded yet</div>
      <div className="text-slate-600 text-sm max-w-xs">
        Run your first investigation and metrics will appear here automatically.
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function MTTRDashboard() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [pil, setPil] = useState<PILMetrics | null>(null)
  const [decisionMetrics, setDecisionMetrics] = useState<DecisionMetrics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastFetch, setLastFetch] = useState(new Date())
  const [windowHours, setWindowHours] = useState(168)  // 7 days default

  const fetchData = useCallback(async () => {
    try {
      const token = localStorage.getItem('agui_token')
      const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {}
      const [mttrRes, pilRes, dmRes] = await Promise.all([
        fetch(`/api/v1/metrics/mttr?window_hours=${windowHours}`, { headers }),
        fetch('/api/v1/metrics/intelligence', { headers }),
        fetch('/api/v1/intelligence/decision-metrics', { headers }),
      ])
      if (!mttrRes.ok) throw new Error(`HTTP ${mttrRes.status}`)
      const json = await mttrRes.json()
      setData(json)
      if (pilRes.ok) setPil(await pilRes.json())
      if (dmRes.ok) setDecisionMetrics(await dmRes.json())
      setError(null)
      setLastFetch(new Date())
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [windowHours])

  useEffect(() => {
    fetchData()
    const id = setInterval(fetchData, 60_000)
    return () => clearInterval(id)
  }, [fetchData])

  const kpis = data?.kpis
  const hasData = kpis && kpis.total_investigations > 0

  return (
    <div className="flex flex-col h-full overflow-y-auto bg-slate-950">
      {/* Header */}
      <div className="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-slate-800 px-6 py-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-100 flex items-center gap-2">
              <Clock size={18} className="text-blue-400" />
              MTTR Dashboard
            </h1>
            <p className="text-xs text-slate-500 mt-0.5">
              Agent performance telemetry — updated {formatDistanceToNow(lastFetch, { addSuffix: true })}
            </p>
          </div>

          <div className="flex items-center gap-3">
            {/* Window selector */}
            <div className="flex rounded-lg border border-slate-700 overflow-hidden text-xs">
              {([24, 168, 720] as const).map(h => (
                <button
                  key={h}
                  onClick={() => setWindowHours(h)}
                  className={clsx(
                    'px-3 py-1.5 transition-colors',
                    windowHours === h
                      ? 'bg-blue-700 text-white'
                      : 'bg-slate-900 text-slate-400 hover:text-slate-200'
                  )}
                >
                  {h === 24 ? '24h' : h === 168 ? '7d' : '30d'}
                </button>
              ))}
            </div>

            <button
              onClick={() => { setLoading(true); fetchData() }}
              disabled={loading}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-slate-800 hover:bg-slate-700
                         text-slate-300 text-xs rounded-lg transition-colors disabled:opacity-50"
            >
              <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
              Refresh
            </button>
          </div>
        </div>
      </div>

      <div className="flex-1 px-6 py-5 space-y-6">
        {error && (
          <div className="flex items-center gap-2 text-sm text-red-400 bg-red-900/20 border border-red-800/40 rounded-lg px-4 py-3">
            <AlertCircle size={14} />
            Failed to load metrics: {error}
          </div>
        )}

        {!hasData && !loading ? (
          <EmptyState />
        ) : (
          <>
            {/* KPI row */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <KPICard
                label="Median MTTR"
                value={kpis ? msToHuman(kpis.mttr_median_ms) : '—'}
                sub={kpis ? `P95: ${msToHuman(kpis.mttr_p95_ms)}` : undefined}
                icon={<Clock size={16} />}
                color="blue"
              />
              <KPICard
                label="RCA Success Rate"
                value={kpis ? pct(kpis.root_cause_found_rate) : '—'}
                sub={kpis ? `${kpis.total_investigations} total investigations` : undefined}
                icon={<CheckCircle2 size={16} />}
                color="green"
              />
              <KPICard
                label="Avg Confidence"
                value={kpis ? `${kpis.mean_confidence.toFixed(0)}%` : '—'}
                sub={kpis ? `Fix proposed: ${pct(kpis.fix_proposed_rate)}` : undefined}
                icon={<TrendingDown size={16} />}
                color="purple"
              />
              <KPICard
                label="Last 24h"
                value={kpis ? String(kpis.last_24h_count) : '—'}
                sub={kpis ? `7d: ${kpis.last_7d_count} investigations` : undefined}
                icon={<BarChart2 size={16} />}
                color="orange"
              />
            </div>

            {/* ROI Banner */}
            {data?.roi && data.roi.investigations > 0 && (
              <ROIBanner roi={data.roi} />
            )}

            {/* Charts row */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div className="bg-slate-900 rounded-xl border border-slate-800 p-5">
                <h2 className="text-sm font-semibold text-slate-200 mb-4">Trend</h2>
                {data?.trend ? <TrendChart data={data.trend} /> : (
                  <div className="h-40 flex items-center justify-center text-slate-600 text-sm">Loading…</div>
                )}
              </div>

              <div className="bg-slate-900 rounded-xl border border-slate-800 p-5">
                <h2 className="text-sm font-semibold text-slate-200 mb-4">Confidence Calibration</h2>
                {data?.calibration ? <CalibrationChart data={data.calibration} /> : (
                  <div className="h-40 flex items-center justify-center text-slate-600 text-sm">Loading…</div>
                )}
              </div>
            </div>

            {/* Service breakdown */}
            <div className="bg-slate-900 rounded-xl border border-slate-800">
              <div className="px-5 py-4 border-b border-slate-800 flex items-center justify-between">
                <h2 className="text-sm font-semibold text-slate-200">Per-Service Breakdown</h2>
                <span className="text-xs text-slate-500">
                  {data?.service_breakdown?.length ?? 0} services — {windowHours / 24}d window
                </span>
              </div>
              <div className="p-1">
                <ServiceTable rows={data?.service_breakdown ?? []} />
              </div>
            </div>

            {/* Pattern Intelligence Layer */}
            {pil && <PILPanel pil={pil} />}

            {/* Intelligence Surface Widgets (decision-metrics) */}
            {decisionMetrics && (
              <div className="space-y-3">
                <div className="flex items-center gap-2 pt-2">
                  <Activity size={14} className="text-slate-500" />
                  <span className="text-xs text-slate-500 uppercase tracking-wider font-medium">Intelligence Surfaces</span>
                </div>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                  <PatternIntelWidget dm={decisionMetrics} />
                  <FeedbackQualityWidget dm={decisionMetrics} />
                  <ConvergenceWidget dm={decisionMetrics} />
                  <AdaptiveIntelWidget dm={decisionMetrics} />
                  <EvidenceDiversityWidget dm={decisionMetrics} />
                  <LearningSafetyWidget dm={decisionMetrics} />
                </div>
                <SourceWeightWidget dm={decisionMetrics} />
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
