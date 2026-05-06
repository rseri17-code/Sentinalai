import { useEffect, useState } from 'react'
import {
  Brain, RefreshCw, CheckCircle2, AlertTriangle, TrendingUp,
  TrendingDown, Minus, Zap, Database, Clock, ChevronDown, ChevronUp,
} from 'lucide-react'
import { useInvestigationStore } from '@/store/investigationStore'
import type { HarnessStatus, HarnessCorrectionRecord } from '@/types'

function QualityBar({ score, label }: { score: number; label: string }) {
  const pct = Math.round(score * 100)
  const color =
    score >= 0.7 ? 'bg-green-500' :
    score >= 0.5 ? 'bg-yellow-500' :
    'bg-red-500'
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-slate-400">
        <span>{label}</span>
        <span className={score >= 0.7 ? 'text-green-400' : score >= 0.5 ? 'text-yellow-400' : 'text-red-400'}>
          {pct}%
        </span>
      </div>
      <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function CorrectionCard({ correction }: { correction: HarnessCorrectionRecord }) {
  const delta = correction.score_after - correction.score_before
  const Icon = delta > 0.01 ? TrendingUp : delta < -0.01 ? TrendingDown : Minus
  const color = delta > 0.01 ? 'text-green-400' : delta < -0.01 ? 'text-red-400' : 'text-slate-400'

  return (
    <div className="bg-slate-800/60 rounded-lg p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-slate-300">Round {correction.round}</span>
        <div className={`flex items-center gap-1 text-xs ${color}`}>
          <Icon size={12} />
          {delta > 0 ? '+' : ''}{(delta * 100).toFixed(1)}%
        </div>
      </div>
      <div className="flex items-center gap-2 text-xs text-slate-500">
        <span>{(correction.score_before * 100).toFixed(0)}%</span>
        <span>→</span>
        <span className={correction.improved ? 'text-green-400' : 'text-slate-400'}>
          {(correction.score_after * 100).toFixed(0)}%
        </span>
        <span className="ml-auto">{correction.gap_queries_run} queries</span>
      </div>
      {correction.gaps_addressed.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {correction.gaps_addressed.slice(0, 3).map((g, i) => (
            <span key={i} className="text-xs bg-slate-700 text-slate-400 px-1.5 py-0.5 rounded truncate max-w-[120px]">
              {g}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    OK: 'bg-green-500',
    WARNING: 'bg-yellow-500',
    DEGRADED: 'bg-red-500',
    UNKNOWN: 'bg-slate-500',
  }
  return <span className={`inline-block w-2 h-2 rounded-full ${colors[status] ?? 'bg-slate-500'}`} />
}

export function ReflectionPanel() {
  const { harnessReflection, harnessPhase, investigationId } = useInvestigationStore()
  const [status, setStatus] = useState<HarnessStatus | null>(null)
  const [showCorrections, setShowCorrections] = useState(false)

  useEffect(() => {
    import('@/api/client').then(({ harnessApi }) => {
      harnessApi.getStatus().then(setStatus).catch(() => null)
    })
  }, [investigationId])

  const isActive = harnessPhase && harnessPhase !== 'reflection_complete'

  return (
    <div className="flex flex-col h-full overflow-y-auto p-4 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain size={16} className="text-violet-400" />
          <h2 className="text-sm font-semibold text-slate-200">Self-Awareness</h2>
        </div>
        {isActive && (
          <div className="flex items-center gap-1.5 text-xs text-violet-400">
            <RefreshCw size={11} className="animate-spin" />
            {harnessPhase === 'pre_flight' ? 'Loading context…' :
             harnessPhase === 'correcting' ? 'Self-correcting…' :
             'Reflecting…'}
          </div>
        )}
        {status && (
          <div className="flex items-center gap-1.5 text-xs text-slate-500">
            <StatusDot status={status.overall_status} />
            {status.overall_status}
          </div>
        )}
      </div>

      {/* No reflection yet */}
      {!harnessReflection && !isActive && (
        <div className="panel p-6 text-center text-slate-500 text-sm">
          <Brain size={24} className="mx-auto mb-2 text-slate-600" />
          Reflection data will appear here once an investigation completes with harness mode enabled.
        </div>
      )}

      {/* Active phase indicator */}
      {isActive && !harnessReflection && (
        <div className="panel p-4 space-y-3">
          <div className="flex items-center gap-2 text-sm text-violet-300">
            <RefreshCw size={14} className="animate-spin" />
            Harness in progress…
          </div>
          <p className="text-xs text-slate-500">
            The agent is evaluating its own output and may self-correct before returning results.
          </p>
        </div>
      )}

      {/* Reflection data */}
      {harnessReflection && (
        <>
          {/* Quality trajectory */}
          <div className="panel p-4 space-y-3">
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider">Quality</div>
            <QualityBar score={harnessReflection.initial_quality} label="Initial" />
            <QualityBar score={harnessReflection.final_quality} label="Final" />
            <div className="flex items-center justify-between text-xs text-slate-500 pt-1">
              <span>{harnessReflection.rounds_run} round{harnessReflection.rounds_run !== 1 ? 's' : ''}</span>
              {harnessReflection.stuck && (
                <span className="text-yellow-500 flex items-center gap-1">
                  <AlertTriangle size={10} />
                  Plateau detected
                </span>
              )}
              {!harnessReflection.stuck && harnessReflection.final_quality >= 0.7 && (
                <span className="text-green-400 flex items-center gap-1">
                  <CheckCircle2 size={10} />
                  Gate met
                </span>
              )}
            </div>
          </div>

          {/* Confidence calibration */}
          <div className="panel p-4 space-y-2">
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider">Confidence</div>
            <div className="flex items-center gap-3">
              <div className="text-center">
                <div className="text-xl font-mono font-bold text-slate-400">
                  {harnessReflection.confidence_raw}%
                </div>
                <div className="text-xs text-slate-600">raw</div>
              </div>
              <div className="text-slate-600">→</div>
              <div className="text-center">
                <div className="text-xl font-mono font-bold text-violet-300">
                  {harnessReflection.confidence_calibrated}%
                </div>
                <div className="text-xs text-slate-500">calibrated</div>
              </div>
              <div className="ml-auto text-right">
                <div className="text-xs text-slate-500">ECE</div>
                <div className="text-sm font-mono text-slate-400">
                  {harnessReflection.calibration_ece.toFixed(3)}
                </div>
              </div>
            </div>
          </div>

          {/* Pre-flight context */}
          <div className="panel p-4 space-y-2">
            <div className="text-xs font-medium text-slate-400 uppercase tracking-wider">Pre-flight Context</div>
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="bg-slate-800/50 rounded p-2">
                <div className="text-slate-500">Experience matches</div>
                <div className="text-slate-200 font-medium mt-0.5">
                  {harnessReflection.experience_matches}
                </div>
              </div>
              <div className="bg-slate-800/50 rounded p-2">
                <div className="text-slate-500">Strategy quality</div>
                <div className="text-slate-200 font-medium mt-0.5">
                  {harnessReflection.strategy_quality != null
                    ? `${(harnessReflection.strategy_quality * 100).toFixed(0)}%`
                    : '—'}
                </div>
              </div>
            </div>
            {harnessReflection.similar_incident_types.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">
                {harnessReflection.similar_incident_types.map((t) => (
                  <span key={t} className="text-xs bg-violet-900/40 text-violet-300 px-1.5 py-0.5 rounded">
                    {t}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* Self-correction rounds */}
          {harnessReflection.corrections.length > 0 && (
            <div className="panel p-4 space-y-2">
              <button
                className="flex items-center justify-between w-full text-xs font-medium text-slate-400 uppercase tracking-wider"
                onClick={() => setShowCorrections(!showCorrections)}
              >
                <span>Self-corrections ({harnessReflection.corrections.length})</span>
                {showCorrections ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              </button>
              {showCorrections && (
                <div className="space-y-2 pt-1">
                  {harnessReflection.corrections.map((c) => (
                    <CorrectionCard key={c.round} correction={c} />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Narrative */}
          {harnessReflection.narrative && (
            <div className="panel p-4 space-y-2">
              <div className="text-xs font-medium text-slate-400 uppercase tracking-wider">Agent Reflection</div>
              <p className="text-xs text-slate-300 leading-relaxed">{harnessReflection.narrative}</p>
            </div>
          )}

          {/* Post-flight badges */}
          <div className="flex flex-wrap gap-2">
            {harnessReflection.learning_updated && (
              <span className="flex items-center gap-1 text-xs bg-green-900/30 text-green-400 px-2 py-1 rounded-full">
                <Zap size={10} />
                Learning updated
              </span>
            )}
            {harnessReflection.experience_stored && (
              <span className="flex items-center gap-1 text-xs bg-blue-900/30 text-blue-400 px-2 py-1 rounded-full">
                <Database size={10} />
                Experience stored
              </span>
            )}
            <span className="flex items-center gap-1 text-xs bg-slate-800 text-slate-500 px-2 py-1 rounded-full">
              <Clock size={10} />
              {Math.round(harnessReflection.elapsed_ms)}ms
            </span>
          </div>
        </>
      )}

      {/* Learning stack health */}
      {status && (
        <div className="panel p-4 space-y-3">
          <div className="text-xs font-medium text-slate-400 uppercase tracking-wider">Learning Stack</div>
          {status.components.confidence_calibrator && (
            <div className="flex items-center justify-between text-xs">
              <span className="text-slate-500">Calibrator</span>
              <div className="flex items-center gap-2">
                <span className="text-slate-400">
                  ECE {status.components.confidence_calibrator.ece.toFixed(3)}
                </span>
                <span className="text-slate-600">
                  {status.components.confidence_calibrator.total_samples} samples
                </span>
                {status.components.confidence_calibrator.stale && (
                  <AlertTriangle size={10} className="text-yellow-500" />
                )}
              </div>
            </div>
          )}
          {status.components.experience_store && (
            <div className="flex items-center justify-between text-xs">
              <span className="text-slate-500">Experiences</span>
              <span className="text-slate-400">
                {status.components.experience_store.total} stored
                {status.components.experience_store.avg_quality > 0 && (
                  <span className="text-slate-600 ml-1">
                    (avg {(status.components.experience_store.avg_quality * 100).toFixed(0)}%)
                  </span>
                )}
              </span>
            </div>
          )}
          {status.components.adaptive_thresholds && status.components.adaptive_thresholds.drifted_count > 0 && (
            <div className="flex items-center justify-between text-xs">
              <span className="text-slate-500">Drift</span>
              <span className="text-yellow-400">
                {status.components.adaptive_thresholds.drifted_count} threshold(s) drifted
              </span>
            </div>
          )}
          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-500">Max correction rounds</span>
            <span className="text-slate-400">{status.max_rounds}</span>
          </div>
          <div className="flex items-center justify-between text-xs">
            <span className="text-slate-500">Quality gate</span>
            <span className="text-slate-400">{Math.round(status.quality_gate * 100)}%</span>
          </div>
        </div>
      )}
    </div>
  )
}
