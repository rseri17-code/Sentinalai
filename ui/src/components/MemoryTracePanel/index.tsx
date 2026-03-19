// Panel 4: Memory Trace Panel
// Shows similar incidents from AgentCore Memory with similarity scores + memory scoring dashboard

import React, { useEffect, useState } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import { memoryApi } from '@/api/client'
import type { MemoryMatch, MemoryScoringResponse } from '@/types'
import { RadarChart, PolarGrid, PolarAngleAxis, Radar, ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip } from 'recharts'
import clsx from 'clsx'
import { Brain, Clock, Filter } from 'lucide-react'

function SimilarityBar({ score }: { score: number }) {
  const color = score > 0.8 ? 'bg-green-500' : score > 0.6 ? 'bg-yellow-500' : 'bg-slate-600'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all', color)} style={{ width: `${score * 100}%` }} />
      </div>
      <span className="text-xs font-mono text-slate-400 w-10 text-right">{(score * 100).toFixed(0)}%</span>
    </div>
  )
}

function MemoryMatchCard({ match }: { match: MemoryMatch }) {
  const [expanded, setExpanded] = useState(false)
  const sourceColors = {
    ltm: 'bg-blue-900/50 text-blue-400',
    stm: 'bg-green-900/50 text-green-400',
    knowledge_graph: 'bg-purple-900/50 text-purple-400',
  }

  return (
    <div className="panel overflow-hidden">
      <button
        className="w-full p-3 text-left hover:bg-slate-800/50 transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-start justify-between gap-2 mb-1.5">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-cyan-400">{match.incident_id}</span>
            <span className={clsx('badge text-[10px]', sourceColors[match.source] || 'bg-slate-800 text-slate-400')}>
              {match.source}
            </span>
          </div>
          {match.occurred_at && (
            <div className="flex items-center gap-1 text-[10px] text-slate-500">
              <Clock size={10} />
              {new Date(match.occurred_at).toLocaleDateString()}
            </div>
          )}
        </div>
        <div className="text-xs text-slate-300 mb-2 line-clamp-2">{match.summary}</div>
        <SimilarityBar score={match.similarity_score} />
      </button>

      {expanded && (
        <div className="border-t border-slate-800 px-3 py-2 space-y-2 text-xs">
          <div>
            <div className="text-slate-500 mb-0.5">Service</div>
            <div className="text-slate-300">{match.service}</div>
          </div>
          {match.root_cause && (
            <div>
              <div className="text-slate-500 mb-0.5">Root Cause</div>
              <div className="text-slate-300">{match.root_cause}</div>
            </div>
          )}
          {match.resolution && (
            <div>
              <div className="text-slate-500 mb-0.5">Resolution</div>
              <div className="text-green-300">{match.resolution}</div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function JudgeScoreRadar({ scores }: { scores: Record<string, number> }) {
  const data = Object.entries(scores).map(([key, value]) => ({
    dimension: key.replace(/_/g, ' '),
    score: Math.round(value * 100),
    fullMark: 100,
  }))

  return (
    <ResponsiveContainer width="100%" height={200}>
      <RadarChart data={data}>
        <PolarGrid stroke="#334155" />
        <PolarAngleAxis dataKey="dimension" tick={{ fill: '#94a3b8', fontSize: 10 }} />
        <Radar
          name="Score"
          dataKey="score"
          stroke="#3b82f6"
          fill="#3b82f6"
          fillOpacity={0.2}
          strokeWidth={1.5}
        />
      </RadarChart>
    </ResponsiveContainer>
  )
}

function BudgetBar({ used, max }: { used: number; max: number }) {
  const pct = max > 0 ? (used / max) * 100 : 0
  const color = pct > 80 ? 'bg-red-500' : pct > 60 ? 'bg-yellow-500' : 'bg-green-500'
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-slate-400">
        <span>Budget: {used}/{max} calls</span>
        <span>{pct.toFixed(0)}%</span>
      </div>
      <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all', color)} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function MemoryTracePanel() {
  const { investigationId, memoryMatches } = useInvestigationStore()
  const [scoring, setScoring] = useState<MemoryScoringResponse | null>(null)
  const [matches, setMatches] = useState<MemoryMatch[]>(memoryMatches)
  const [minScore, setMinScore] = useState(0)
  const [sourceFilter, setSourceFilter] = useState<string | undefined>()
  const [activeTab, setActiveTab] = useState<'matches' | 'scoring'>('matches')

  useEffect(() => {
    if (!investigationId) return
    memoryApi.getTrace(investigationId, { min_score: minScore, source: sourceFilter })
      .then((res) => setMatches(res.matches))
      .catch(console.error)
    memoryApi.getScoring(investigationId)
      .then(setScoring)
      .catch(console.error)
  }, [investigationId, minScore, sourceFilter])

  return (
    <div className="flex flex-col h-full p-4">
      <div className="panel-header rounded-t-lg border border-slate-800 bg-slate-900">
        <div className="flex items-center gap-2">
          <Brain size={14} className="text-cyan-400" />
          <span className="text-sm font-medium text-slate-200">Memory Trace</span>
          <span className="badge bg-slate-800 text-slate-400">{matches.length} matches</span>
        </div>
        <div className="flex gap-1">
          {(['matches', 'scoring'] as const).map((t) => (
            <button
              key={t}
              onClick={() => setActiveTab(t)}
              className={clsx('btn text-xs py-1 capitalize', activeTab === t ? 'bg-slate-700 text-slate-200' : 'btn-ghost')}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {activeTab === 'matches' && (
        <>
          {/* Filters */}
          <div className="border-x border-slate-800 bg-slate-900/50 px-4 py-2 flex items-center gap-3">
            <Filter size={12} className="text-slate-500" />
            <div className="flex items-center gap-2">
              <label className="text-xs text-slate-500">Min score</label>
              <input
                type="range"
                min="0" max="100" step="10"
                value={minScore * 100}
                onChange={(e) => setMinScore(Number(e.target.value) / 100)}
                className="w-20 accent-blue-500"
              />
              <span className="text-xs text-slate-400 w-8">{(minScore * 100).toFixed(0)}%</span>
            </div>
            <div className="flex gap-1">
              {[undefined, 'ltm', 'stm', 'knowledge_graph'].map((s) => (
                <button
                  key={s ?? 'all'}
                  onClick={() => setSourceFilter(s)}
                  className={clsx('btn text-[10px] py-0.5', sourceFilter === s ? 'bg-slate-700 text-slate-200' : 'btn-ghost')}
                >
                  {s ?? 'all'}
                </button>
              ))}
            </div>
          </div>

          <div className="flex-1 scroll-panel border border-t-0 border-slate-800 rounded-b-lg bg-slate-900 p-3 space-y-2">
            {matches.length === 0 ? (
              <div className="text-center text-slate-600 py-8 text-sm">No memory matches found.</div>
            ) : (
              matches.map((m) => <MemoryMatchCard key={m.incident_id} match={m} />)
            )}
          </div>
        </>
      )}

      {activeTab === 'scoring' && scoring && (
        <div className="flex-1 scroll-panel border border-t-0 border-slate-800 rounded-b-lg bg-slate-900 p-4 space-y-4">
          {/* LLM Judge Radar */}
          <div className="panel p-3">
            <div className="text-xs text-slate-500 mb-2 uppercase tracking-wider">LLM Judge Scores</div>
            <JudgeScoreRadar scores={scoring.scoring.judge_scores} />
            <div className="text-center text-xs text-slate-400 mt-1">
              Average: <span className="text-blue-400 font-medium">{(scoring.scoring.judge_average * 100).toFixed(0)}%</span>
            </div>
          </div>

          {/* Budget */}
          <div className="panel p-3">
            <div className="text-xs text-slate-500 mb-2 uppercase tracking-wider">Budget</div>
            <BudgetBar used={scoring.budget.used} max={scoring.budget.max} />
            <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-center">
              <div className="bg-slate-800 rounded p-2">
                <div className="text-green-400 font-medium">{scoring.budget.tool_calls_success}</div>
                <div className="text-slate-500">Success</div>
              </div>
              <div className="bg-slate-800 rounded p-2">
                <div className="text-red-400 font-medium">{scoring.budget.tool_calls_failed}</div>
                <div className="text-slate-500">Failed</div>
              </div>
              <div className="bg-slate-800 rounded p-2">
                <div className="text-slate-300 font-medium">{scoring.budget.tool_calls_total}</div>
                <div className="text-slate-500">Total</div>
              </div>
            </div>
          </div>

          {/* Evidence completeness */}
          <div className="panel p-3">
            <div className="text-xs text-slate-500 mb-2 uppercase tracking-wider">Evidence Completeness</div>
            <div className="flex items-center gap-3">
              <div className="text-2xl font-bold text-blue-400">
                {(scoring.scoring.evidence_completeness * 100).toFixed(0)}%
              </div>
              <div className="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className="h-full bg-blue-500 rounded-full"
                  style={{ width: `${scoring.scoring.evidence_completeness * 100}%` }}
                />
              </div>
            </div>
          </div>

          {/* Confidence */}
          <div className="panel p-3 space-y-2">
            <div className="text-xs text-slate-500 uppercase tracking-wider">Confidence</div>
            <div className="flex items-center gap-3">
              <div className="text-3xl font-bold text-purple-400">
                {(scoring.scoring.confidence * 100).toFixed(0)}%
              </div>
              <div className="text-xs text-slate-400">
                Winner: <span className="text-purple-300">{scoring.scoring.winner_hypothesis}</span>
              </div>
            </div>
          </div>

          {/* Stale sources */}
          {scoring.stale_sources.length > 0 && (
            <div className="panel p-3 border-orange-800/50">
              <div className="text-xs text-orange-400 mb-2">⚠️ Stale Data Sources</div>
              {scoring.stale_sources.map((s) => (
                <span key={s} className="badge bg-orange-900/30 text-orange-400 mr-1 mb-1">{s}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
