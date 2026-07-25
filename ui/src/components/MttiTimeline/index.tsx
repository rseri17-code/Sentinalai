import React, { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Clock, Search, Target, User, Wrench, CheckCircle2 } from 'lucide-react'
import { mttiApi, type MttiResponse } from '@/api/client'

const STEPS: { key: keyof MttiResponse['segments_ms']; label: string; icon: React.ReactNode }[] = [
  { key: 'time_to_first_evidence_ms', label: 'First evidence', icon: <Search size={14} /> },
  { key: 'time_to_root_cause_ms', label: 'Root cause', icon: <Target size={14} /> },
  { key: 'time_to_owner_ms', label: 'Owner', icon: <User size={14} /> },
  { key: 'time_to_recommendation_ms', label: 'Recommendation', icon: <Wrench size={14} /> },
  { key: 'total_ms', label: 'Actionable', icon: <CheckCircle2 size={14} /> },
]

function fmt(ms: number | null): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(1)} s`
}

export function MttiTimeline() {
  const { investigationId } = useParams<{ investigationId: string }>()
  const [data, setData] = useState<MttiResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!investigationId) return
    setLoading(true)
    setError(null)
    mttiApi
      .forInvestigation(investigationId)
      .then(setData)
      .catch((e) => setError(e?.message || 'Failed to load MTTI'))
      .finally(() => setLoading(false))
  }, [investigationId])

  const total = data?.segments_ms.total_ms ?? null
  const max = data
    ? Math.max(1, ...STEPS.map((s) => data.segments_ms[s.key] ?? 0))
    : 1

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0 flex items-center gap-2">
        <Clock size={15} className="text-blue-400" />
        <span className="text-sm font-medium text-slate-300">MTTI — Time to Identify</span>
        <span className="text-[10px] text-slate-500 ml-1">— from recorded investigation events</span>
      </div>

      <div className="flex-1 overflow-auto p-4">
        {loading && <div className="text-slate-500 text-sm">Loading MTTI…</div>}
        {error && !loading && <div className="text-red-400 text-sm">{error}</div>}
        {!loading && !error && data && (
          <>
            <div className="mb-4 text-sm text-slate-400">
              Time to actionable:{' '}
              <span className="text-slate-100 font-semibold">{fmt(total)}</span>
              {!data.actionable && (
                <span className="ml-2 text-xs text-amber-400">root cause not yet identified</span>
              )}
            </div>

            <div className="space-y-2">
              {STEPS.map((step) => {
                const v = data.segments_ms[step.key]
                const pct = v != null ? Math.max(4, (v / max) * 100) : 0
                return (
                  <div key={step.key} className="flex items-center gap-3">
                    <div className="w-36 shrink-0 flex items-center gap-2 text-xs text-slate-400">
                      {step.icon}
                      {step.label}
                    </div>
                    <div className="flex-1 h-4 bg-slate-800/60 rounded overflow-hidden">
                      {v != null && (
                        <div className="h-full bg-blue-600/70" style={{ width: `${pct}%` }} />
                      )}
                    </div>
                    <div className="w-20 shrink-0 text-right text-xs text-slate-300">{fmt(v)}</div>
                  </div>
                )
              })}
            </div>

            <div className="mt-4 text-[11px] text-slate-500">{data.note}</div>
          </>
        )}
      </div>
    </div>
  )
}

export default MttiTimeline
