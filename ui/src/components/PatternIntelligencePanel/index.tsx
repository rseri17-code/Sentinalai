// Pattern Intelligence Panel
// Right column in the investigation 3-column layout and Mission Control.
// Surfaces: memory matches (similar past incidents), real-time intelligence
// events (SLO burns, predictions), and neural blend state.

import React, { useMemo } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import type { MemoryMatch, AGUIEvent } from '@/types'
import { Brain, Zap, TrendingUp, Clock, ChevronRight, Sparkles } from 'lucide-react'
import { fmtDuration } from '@/utils/mtti'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Memory match card
// ---------------------------------------------------------------------------

function MatchCard({ match, rank }: { match: MemoryMatch; rank: number }) {
  const pct = Math.round(match.similarity_score * 100)
  const sourceColor: Record<string, string> = {
    stm: 'text-cyan-400 bg-cyan-900/30 border-cyan-800',
    ltm: 'text-indigo-400 bg-indigo-900/30 border-indigo-800',
    knowledge_graph: 'text-violet-400 bg-violet-900/30 border-violet-800',
  }

  return (
    <div
      className={clsx(
        'rounded border p-2.5 space-y-1.5 text-xs',
        rank === 0
          ? 'border-blue-800/60 bg-blue-950/30'
          : 'border-slate-800 bg-slate-900/40'
      )}
    >
      {/* Top row: similarity + source badge */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          {/* Similarity bar */}
          <div className="w-16 h-1.5 rounded-full bg-slate-800 overflow-hidden">
            <div
              className={clsx(
                'h-full rounded-full',
                pct >= 85 ? 'bg-green-500' : pct >= 70 ? 'bg-yellow-500' : 'bg-slate-500'
              )}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className={clsx('font-mono font-semibold', pct >= 85 ? 'text-green-400' : 'text-slate-400')}>
            {pct}%
          </span>
        </div>
        <span className={clsx('badge border text-[9px]', sourceColor[match.source] || 'text-slate-500 bg-slate-800 border-slate-700')}>
          {match.source}
        </span>
      </div>

      {/* Incident ID + service */}
      <div className="flex items-center gap-1 text-slate-400">
        <span className="font-mono text-blue-400">{match.incident_id}</span>
        {match.service && <span className="text-slate-600">· {match.service}</span>}
      </div>

      {/* Summary */}
      <p className="text-slate-300 leading-tight">{match.summary}</p>

      {/* Root cause + resolution */}
      {match.root_cause && (
        <div className="flex items-start gap-1 text-slate-500">
          <ChevronRight size={10} className="mt-0.5 shrink-0 text-orange-500" />
          <span className="text-slate-400">{match.root_cause}</span>
        </div>
      )}
      {match.resolution && (
        <div className="flex items-start gap-1">
          <ChevronRight size={10} className="mt-0.5 shrink-0 text-green-500" />
          <span className="text-green-300">{match.resolution}</span>
        </div>
      )}

      {/* When */}
      {match.occurred_at && (
        <div className="flex items-center gap-1 text-slate-600">
          <Clock size={9} />
          <span>{new Date(match.occurred_at).toLocaleDateString()}</span>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Intelligence event cards (live WS events)
// ---------------------------------------------------------------------------

function IntelligenceEventCard({ event }: { event: AGUIEvent }) {
  const p = event.payload as Record<string, unknown>

  if (event.event_type === 'intelligence.slo_burning') {
    return (
      <div className="rounded border border-red-800/60 bg-red-950/20 p-2.5 text-xs space-y-1">
        <div className="flex items-center gap-1.5 text-red-400 font-medium">
          <Zap size={11} />
          SLO Burn Alert
        </div>
        {typeof p.slo_name === 'string' && <div className="text-slate-300">{p.slo_name}</div>}
        {typeof p.burn_rate === 'number' && (
          <div className="text-slate-400">
            burn rate: <span className="text-red-300 font-mono">{(p.burn_rate as number).toFixed(2)}×</span>
          </div>
        )}
      </div>
    )
  }

  if (event.event_type === 'intelligence.prediction') {
    return (
      <div className="rounded border border-violet-800/50 bg-violet-950/20 p-2.5 text-xs space-y-1">
        <div className="flex items-center gap-1.5 text-violet-400 font-medium">
          <TrendingUp size={11} />
          Prediction
        </div>
        {typeof p.prediction === 'string' && <div className="text-slate-300">{p.prediction}</div>}
        {typeof p.confidence === 'number' && (
          <div className="text-slate-500">
            confidence: <span className="font-mono">{(p.confidence as number * 100).toFixed(0)}%</span>
          </div>
        )}
      </div>
    )
  }

  return null
}

// ---------------------------------------------------------------------------
// Section header
// ---------------------------------------------------------------------------

function SectionHeader({ icon, label, count }: { icon: React.ReactNode; label: string; count?: number }) {
  return (
    <div className="flex items-center gap-1.5 text-[10px] text-slate-500 uppercase tracking-wider font-medium mb-2">
      {icon}
      {label}
      {count !== undefined && count > 0 && (
        <span className="ml-auto bg-slate-800 text-slate-400 rounded-full px-1.5 py-0">{count}</span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState({ label }: { label: string }) {
  return (
    <div className="text-[11px] text-slate-600 text-center py-3">{label}</div>
  )
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

export interface PatternIntelligencePanelProps {
  /** If provided, used instead of store's memoryMatches (for global/cross-incident view) */
  memoryMatches?: MemoryMatch[]
  /** Optional: show neural blend state */
  showNeuralBlend?: boolean
}

export function PatternIntelligencePanel({
  memoryMatches: propMatches,
  showNeuralBlend = true,
}: PatternIntelligencePanelProps) {
  const { memoryMatches: storeMatches, events } = useInvestigationStore()
  const matches = propMatches ?? storeMatches

  // Intelligence events from the WS stream
  const intelEvents = useMemo(
    () =>
      events
        .filter(
          (e) =>
            e.event_type === 'intelligence.slo_burning' ||
            e.event_type === 'intelligence.prediction'
        )
        .slice(-5)
        .reverse(),
    [events]
  )

  // Pattern summary: cluster by source
  const patternSummary = useMemo(() => {
    if (matches.length === 0) return null
    const topMatch = matches[0]
    const pct = Math.round(topMatch.similarity_score * 100)
    return pct >= 70 ? { topMatch, pct } : null
  }, [matches])

  return (
    <div className="flex flex-col gap-4 text-xs h-full overflow-y-auto">

      {/* ── Pattern alert banner ── */}
      {patternSummary && (
        <div className="rounded border border-blue-800/50 bg-blue-950/20 p-2.5 space-y-1">
          <div className="flex items-center gap-1.5 font-medium text-blue-300">
            <Sparkles size={11} />
            Pattern Match
          </div>
          <p className="text-slate-300 leading-tight">{patternSummary.topMatch.summary}</p>
          <div className="text-slate-500">
            {patternSummary.pct}% similar · {patternSummary.topMatch.source}
          </div>
          {patternSummary.topMatch.resolution && (
            <div className="text-green-300 text-[10px]">
              ✓ {patternSummary.topMatch.resolution}
            </div>
          )}
        </div>
      )}

      {/* ── Live intelligence events ── */}
      {intelEvents.length > 0 && (
        <div>
          <SectionHeader icon={<Zap size={10} />} label="Live Signals" count={intelEvents.length} />
          <div className="space-y-2">
            {intelEvents.map((e) => (
              <IntelligenceEventCard key={e.event_id} event={e} />
            ))}
          </div>
        </div>
      )}

      {/* ── Memory matches ── */}
      <div>
        <SectionHeader
          icon={<Brain size={10} />}
          label="Similar Incidents"
          count={matches.length}
        />
        {matches.length === 0 ? (
          <EmptyState label="No similar incidents found yet" />
        ) : (
          <div className="space-y-2">
            {matches.slice(0, 5).map((m, i) => (
              <MatchCard key={m.incident_id} match={m} rank={i} />
            ))}
            {matches.length > 5 && (
              <p className="text-slate-600 text-center text-[10px]">
                +{matches.length - 5} more in Memory Trace
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
