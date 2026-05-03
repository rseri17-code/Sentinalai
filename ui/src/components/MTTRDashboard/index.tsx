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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastFetch, setLastFetch] = useState(new Date())
  const [windowHours, setWindowHours] = useState(168)  // 7 days default

  const fetchData = useCallback(async () => {
    try {
      const token = localStorage.getItem('agui_token')
      const res = await fetch(`/api/v1/metrics/mttr?window_hours=${windowHours}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const json = await res.json()
      setData(json)
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
          </>
        )}
      </div>
    </div>
  )
}
