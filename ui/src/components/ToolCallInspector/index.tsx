import { useEffect, useState, useCallback } from 'react'
import {
  Wrench, ChevronDown, ChevronRight, CheckCircle2, XCircle, Clock,
  AlertTriangle, Activity, TrendingUp, TrendingDown, Minus, Zap,
  Database, Eye, RefreshCw,
} from 'lucide-react'
import { useInvestigationStore } from '@/store/investigationStore'
import { transparencyApi } from '@/api/client'
import type { EnrichedToolReceipt, SignalFact, HypothesisDelta } from '@/types'

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusIcon({ status }: { status: string }) {
  if (status === 'success') return <CheckCircle2 size={12} className="text-green-400 shrink-0" />
  if (status === 'timeout') return <Clock size={12} className="text-yellow-400 shrink-0" />
  return <XCircle size={12} className="text-red-400 shrink-0" />
}

function PhaseChip({ phase }: { phase: string }) {
  const colors: Record<string, string> = {
    collect: 'bg-blue-900/40 text-blue-300',
    analyze: 'bg-violet-900/40 text-violet-300',
    classify: 'bg-amber-900/40 text-amber-300',
    playbook: 'bg-teal-900/40 text-teal-300',
  }
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded ${colors[phase] ?? 'bg-slate-800 text-slate-400'}`}>
      {phase}
    </span>
  )
}

function LatencyBar({ ms }: { ms: number }) {
  // Cap visual at 5000ms
  const pct = Math.min(100, (ms / 5000) * 100)
  const color = ms < 500 ? 'bg-green-500' : ms < 2000 ? 'bg-yellow-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2 w-24">
      <div className="flex-1 h-1 bg-slate-700 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-slate-500 tabular-nums w-12 text-right">{Math.round(ms)}ms</span>
    </div>
  )
}

function SignalBar({ count, noise }: { count: number; noise: number }) {
  const signal = Math.max(0, 1 - noise)
  return (
    <div className="flex items-center gap-1.5">
      <Activity size={10} className={signal > 0.5 ? 'text-green-400' : 'text-slate-500'} />
      <div className="w-12 h-1 bg-slate-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-emerald-500 rounded-full"
          style={{ width: `${signal * 100}%` }}
        />
      </div>
      <span className="text-xs text-slate-500">{count}sig</span>
    </div>
  )
}

function SignalFactPill({ fact }: { fact: SignalFact }) {
  const colors: Record<string, string> = {
    error: 'bg-red-900/40 text-red-300 border border-red-800/30',
    anomaly: 'bg-orange-900/40 text-orange-300 border border-orange-800/30',
    change: 'bg-yellow-900/40 text-yellow-300 border border-yellow-800/30',
    threshold: 'bg-amber-900/40 text-amber-300 border border-amber-800/30',
    service: 'bg-blue-900/40 text-blue-300 border border-blue-800/30',
    generic: 'bg-slate-800 text-slate-400',
  }
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded truncate max-w-[200px] ${colors[fact.category] ?? colors.generic}`}>
      {fact.text}
    </span>
  )
}

function HypothesisDeltaRow({ delta }: { delta: HypothesisDelta }) {
  const d = delta.delta
  const Icon = d > 1 ? TrendingUp : d < -1 ? TrendingDown : Minus
  const color = d > 1 ? 'text-green-400' : d < -1 ? 'text-red-400' : 'text-slate-400'
  return (
    <div className="flex items-center justify-between text-xs py-0.5">
      <span className="text-slate-400 truncate max-w-[160px]">{delta.name}</span>
      <div className={`flex items-center gap-1 ${color} shrink-0`}>
        <Icon size={10} />
        <span className="tabular-nums">
          {delta.score_before.toFixed(0)} → {delta.score_after.toFixed(0)}
        </span>
      </div>
    </div>
  )
}

function ReceiptRow({
  receipt,
  expanded,
  onToggle,
}: {
  receipt: EnrichedToolReceipt
  expanded: boolean
  onToggle: () => void
}) {
  return (
    <div className="border-b border-slate-800/50">
      {/* Summary row */}
      <button
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-slate-800/30 text-left group transition-colors"
        onClick={onToggle}
      >
        <StatusIcon status={receipt.status} />
        <span className="text-slate-300 text-xs font-mono flex-1 truncate">
          {receipt.worker}
          <span className="text-slate-500">.</span>
          {receipt.action}
        </span>
        <PhaseChip phase={receipt.phase} />
        <SignalBar count={receipt.signal_count} noise={receipt.noise_ratio} />
        <LatencyBar ms={receipt.latency_ms} />
        {expanded ? (
          <ChevronDown size={12} className="text-slate-500 shrink-0" />
        ) : (
          <ChevronRight size={12} className="text-slate-500 shrink-0" />
        )}
      </button>

      {/* Expanded detail — 4 layers */}
      {expanded && (
        <div className="px-3 pb-3 space-y-3 border-t border-slate-800/50 pt-3">
          {/* Layer 1: Intent */}
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs font-medium text-slate-500 uppercase tracking-wider">
              <Eye size={10} />
              Intent
            </div>
            <p className="text-xs text-slate-300">{receipt.intent_summary || `${receipt.worker}.${receipt.action}`}</p>
          </div>

          {/* Layer 2: The Call */}
          <div className="space-y-1">
            <div className="flex items-center gap-1.5 text-xs font-medium text-slate-500 uppercase tracking-wider">
              <Wrench size={10} />
              Call Params
            </div>
            <pre className="text-xs text-slate-400 bg-slate-900/60 rounded p-2 overflow-x-auto max-h-24 scrollbar-thin">
              {JSON.stringify(receipt.params, null, 2)}
            </pre>
          </div>

          {/* Layer 3: Response */}
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5 text-xs font-medium text-slate-500 uppercase tracking-wider">
              <Database size={10} />
              Response — {receipt.result_count} results · {Math.round((1 - receipt.noise_ratio) * 100)}% signal
            </div>
            {receipt.status !== 'success' && receipt.error_msg && (
              <div className="flex items-center gap-1.5 text-xs text-red-400 bg-red-900/20 rounded p-2">
                <AlertTriangle size={10} />
                {receipt.error_msg}
              </div>
            )}
            {receipt.signal_facts.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {receipt.signal_facts.slice(0, 8).map((f, i) => (
                  <SignalFactPill key={i} fact={f} />
                ))}
                {receipt.signal_facts.length > 8 && (
                  <span className="text-xs text-slate-600">+{receipt.signal_facts.length - 8} more</span>
                )}
              </div>
            )}
            {receipt.signal_facts.length === 0 && receipt.status === 'success' && (
              <span className="text-xs text-slate-600 italic">No notable signals extracted</span>
            )}
          </div>

          {/* Layer 4: Influence */}
          {receipt.hypothesis_deltas.length > 0 && (
            <div className="space-y-1">
              <div className="flex items-center gap-1.5 text-xs font-medium text-slate-500 uppercase tracking-wider">
                <Zap size={10} />
                Hypothesis Influence
                {receipt.confidence_delta !== 0 && (
                  <span className={`ml-auto font-mono ${receipt.confidence_delta > 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {receipt.confidence_delta > 0 ? '+' : ''}{receipt.confidence_delta.toFixed(1)}% confidence
                  </span>
                )}
              </div>
              <div className="space-y-0.5">
                {receipt.hypothesis_deltas.map((d) => (
                  <HypothesisDeltaRow key={d.name} delta={d} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export function ToolCallInspector() {
  const { investigationId } = useInvestigationStore()
  const [receipts, setReceipts] = useState<EnrichedToolReceipt[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set())
  const [filterPhase, setFilterPhase] = useState<string>('')
  const [filterStatus, setFilterStatus] = useState<string>('')
  const [autoRefresh, setAutoRefresh] = useState(true)

  const load = useCallback(async () => {
    if (!investigationId) return
    setLoading(true)
    try {
      const res = await transparencyApi.listToolCalls(investigationId, {
        phase: filterPhase || undefined,
        status: filterStatus || undefined,
        limit: 200,
      })
      setReceipts(res.items)
      setTotal(res.total)
    } catch {
      // silently ignore — investigation may not have started yet
    } finally {
      setLoading(false)
    }
  }, [investigationId, filterPhase, filterStatus])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!autoRefresh) return
    const id = setInterval(load, 3000)
    return () => clearInterval(id)
  }, [autoRefresh, load])

  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const successCount = receipts.filter((r) => r.status === 'success').length
  const errorCount = receipts.filter((r) => r.status === 'error').length
  const timeoutCount = receipts.filter((r) => r.status === 'timeout').length
  const avgLatency = receipts.length > 0
    ? receipts.reduce((s, r) => s + r.latency_ms, 0) / receipts.length
    : 0

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
        <div className="flex items-center gap-2">
          <Wrench size={15} className="text-cyan-400" />
          <h2 className="text-sm font-semibold text-slate-200">Tool Call Inspector</h2>
          <span className="text-xs text-slate-500">{total} calls</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setAutoRefresh((v) => !v)}
            className={`flex items-center gap-1 text-xs px-2 py-1 rounded ${autoRefresh ? 'text-cyan-400' : 'text-slate-500'}`}
            title={autoRefresh ? 'Auto-refresh on' : 'Auto-refresh off'}
          >
            <RefreshCw size={11} className={autoRefresh ? 'animate-spin' : ''} />
          </button>
          <button onClick={load} className="text-xs text-slate-500 hover:text-slate-300 px-2 py-1">
            Refresh
          </button>
        </div>
      </div>

      {/* Summary stats */}
      <div className="grid grid-cols-4 gap-px border-b border-slate-800 bg-slate-800/30">
        {[
          { label: 'Success', value: successCount, color: 'text-green-400' },
          { label: 'Error', value: errorCount, color: 'text-red-400' },
          { label: 'Timeout', value: timeoutCount, color: 'text-yellow-400' },
          { label: 'Avg ms', value: Math.round(avgLatency), color: 'text-slate-300' },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-slate-900/50 px-3 py-2 text-center">
            <div className={`text-base font-mono font-bold ${color}`}>{value}</div>
            <div className="text-xs text-slate-600">{label}</div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-800 bg-slate-900/30">
        <select
          className="text-xs bg-slate-800 text-slate-300 border border-slate-700 rounded px-2 py-1"
          value={filterPhase}
          onChange={(e) => setFilterPhase(e.target.value)}
        >
          <option value="">All phases</option>
          <option value="collect">collect</option>
          <option value="analyze">analyze</option>
          <option value="classify">classify</option>
          <option value="playbook">playbook</option>
        </select>
        <select
          className="text-xs bg-slate-800 text-slate-300 border border-slate-700 rounded px-2 py-1"
          value={filterStatus}
          onChange={(e) => setFilterStatus(e.target.value)}
        >
          <option value="">All status</option>
          <option value="success">success</option>
          <option value="error">error</option>
          <option value="timeout">timeout</option>
        </select>
      </div>

      {/* Waterfall list */}
      <div className="flex-1 overflow-y-auto">
        {!investigationId && (
          <div className="p-6 text-center text-slate-500 text-sm">
            <Wrench size={24} className="mx-auto mb-2 text-slate-600" />
            Open an investigation to inspect tool calls.
          </div>
        )}

        {investigationId && !loading && receipts.length === 0 && (
          <div className="p-6 text-center text-slate-500 text-sm">
            No tool calls recorded yet.
          </div>
        )}

        {receipts.map((r) => (
          <ReceiptRow
            key={r.receipt_id}
            receipt={r}
            expanded={expandedIds.has(r.receipt_id)}
            onToggle={() => toggle(r.receipt_id)}
          />
        ))}
      </div>
    </div>
  )
}
