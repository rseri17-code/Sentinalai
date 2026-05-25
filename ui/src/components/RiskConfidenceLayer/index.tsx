// Always-visible Risk + Confidence Layer
// Shows at the top of every investigation view
// Updates in real-time via WebSocket events

import React, { useEffect, useState } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import { investigationsApi } from '@/api/client'
import type { RiskConfidence } from '@/types'
import { AlertTriangle, TrendingUp, Clock, Zap } from 'lucide-react'
import clsx from 'clsx'

const RISK_COLORS: Record<string, string> = {
  critical: 'text-red-400 bg-red-900/30 border-red-800',
  high: 'text-orange-400 bg-orange-900/30 border-orange-800',
  medium: 'text-yellow-400 bg-yellow-900/30 border-yellow-800',
  low: 'text-green-400 bg-green-900/30 border-green-800',
  unknown: 'text-slate-400 bg-slate-800 border-slate-700',
}

function ConfidenceGauge({ value }: { value: number }) {
  const pct = value * 100
  const color = value > 0.8 ? '#22c55e' : value > 0.6 ? '#eab308' : value > 0.4 ? '#f97316' : '#ef4444'

  return (
    <div className="flex items-center gap-2">
      <div className="relative w-10 h-10">
        <svg className="w-10 h-10 -rotate-90" viewBox="0 0 36 36">
          <circle cx="18" cy="18" r="14" fill="none" stroke="#1e293b" strokeWidth="3" />
          <circle
            cx="18" cy="18" r="14"
            fill="none"
            stroke={color}
            strokeWidth="3"
            strokeDasharray={`${pct * 0.879} 87.9`}
            strokeLinecap="round"
          />
        </svg>
        <span className="absolute inset-0 flex items-center justify-center text-[9px] font-bold" style={{ color }}>
          {pct.toFixed(0)}%
        </span>
      </div>
      <div>
        <div className="text-[10px] text-slate-500">Confidence</div>
        <div className="text-xs font-medium" style={{ color }}>{pct.toFixed(0)}%</div>
      </div>
    </div>
  )
}

function StaleDataWarning({ sources }: { sources: string[] }) {
  if (sources.length === 0) return null
  return (
    <div className="flex items-center gap-1.5 bg-orange-900/30 border border-orange-800 rounded px-2 py-1 text-xs text-orange-300">
      <Clock size={11} />
      <span>Stale: {sources.slice(0, 3).join(', ')}{sources.length > 3 ? ` +${sources.length - 3}` : ''}</span>
    </div>
  )
}

export function RiskConfidenceLayer({ compact }: { compact?: boolean } = {}) {
  const { investigationId, investigation, latestEvent } = useInvestigationStore()
  const [risk, setRisk] = useState<RiskConfidence | null>(null)

  useEffect(() => {
    if (!investigationId) return
    investigationsApi.getRisk(investigationId)
      .then(setRisk)
      .catch(console.error)
  }, [investigationId, latestEvent?.event_type])

  if (!investigation && !risk) return null

  const confidence = risk?.confidence ?? investigation?.confidence ?? 0
  const riskLevel = risk?.risk_level ?? investigation?.risk_level ?? 'unknown'
  const stale = risk?.stale_sources ?? investigation?.stale_sources ?? []
  const budgetUsed = risk?.budget_used ?? investigation?.budget_used ?? 0
  const budgetMax = risk?.budget_max ?? investigation?.budget_max ?? 20
  const budgetPct = budgetMax > 0 ? (budgetUsed / budgetMax) * 100 : 0
  const judgeScores = risk?.judge_scores ?? investigation?.judge_scores ?? {}
  const avgJudge = Object.values(judgeScores).length > 0
    ? Object.values(judgeScores).reduce((a, b) => a + b, 0) / Object.values(judgeScores).length
    : 0

  // Compact: vertical card for left column in 3-col layout
  if (compact) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/60 p-2.5 space-y-2 text-xs">
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-slate-500 uppercase tracking-wider font-medium">Risk</span>
          <span className={clsx('badge border text-[9px]', RISK_COLORS[riskLevel] || RISK_COLORS.unknown)}>
            {riskLevel}
          </span>
        </div>
        {avgJudge > 0 && (
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-slate-500">Judge</span>
            <span className="font-mono text-blue-400">{(avgJudge * 100).toFixed(0)}%</span>
          </div>
        )}
        {stale.length > 0 && (
          <div className="flex items-center gap-1 text-orange-400 text-[10px]">
            <AlertTriangle size={9} />
            <span>Stale: {stale.slice(0, 2).join(', ')}</span>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="bg-slate-900 border-b border-slate-800 px-4 py-2 flex items-center gap-4 shrink-0 overflow-x-auto">
      {/* Confidence gauge */}
      <ConfidenceGauge value={confidence} />

      <div className="w-px h-8 bg-slate-800 shrink-0" />

      {/* Risk level */}
      <div className="flex items-center gap-1.5">
        <TrendingUp size={13} className="text-slate-500 shrink-0" />
        <div>
          <div className="text-[10px] text-slate-500">Risk</div>
          <span className={clsx('badge border text-[10px]', RISK_COLORS[riskLevel] || RISK_COLORS.unknown)}>
            {riskLevel}
          </span>
        </div>
      </div>

      <div className="w-px h-8 bg-slate-800 shrink-0" />

      {/* Budget */}
      <div className="flex items-center gap-2">
        <Zap size={13} className={clsx('shrink-0', budgetPct > 80 ? 'text-red-400' : 'text-slate-500')} />
        <div>
          <div className="text-[10px] text-slate-500">Budget</div>
          <div className="flex items-center gap-1.5">
            <div className="w-16 h-1.5 bg-slate-800 rounded-full overflow-hidden">
              <div
                className={clsx('h-full rounded-full', budgetPct > 80 ? 'bg-red-500' : budgetPct > 60 ? 'bg-yellow-500' : 'bg-green-500')}
                style={{ width: `${budgetPct}%` }}
              />
            </div>
            <span className="text-xs text-slate-400">{budgetUsed}/{budgetMax}</span>
          </div>
        </div>
      </div>

      {/* Judge average */}
      {avgJudge > 0 && (
        <>
          <div className="w-px h-8 bg-slate-800 shrink-0" />
          <div>
            <div className="text-[10px] text-slate-500">Judge Score</div>
            <div className="text-xs font-mono text-blue-400">{(avgJudge * 100).toFixed(0)}%</div>
          </div>
        </>
      )}

      {/* Stale data warning */}
      {stale.length > 0 && (
        <>
          <div className="w-px h-8 bg-slate-800 shrink-0" />
          <StaleDataWarning sources={stale} />
        </>
      )}

      {/* Pending approval alert */}
      {investigation?.awaiting_approval_for && (
        <>
          <div className="w-px h-8 bg-slate-800 shrink-0" />
          <div className="flex items-center gap-1.5 bg-yellow-900/30 border border-yellow-800 rounded px-2 py-1 text-xs text-yellow-300 animate-pulse">
            <AlertTriangle size={11} />
            <span>Awaiting approval</span>
          </div>
        </>
      )}
    </div>
  )
}
