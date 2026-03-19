// Panel 5: Replay Mode
// Deterministic step-by-step investigation replay with controls

import React, { useEffect } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import type { ReplayStatus } from '@/types'
import { Play, Pause, SkipForward, Square, FastForward, Zap } from 'lucide-react'
import clsx from 'clsx'

const STATUS_LABELS: Record<ReplayStatus, string> = {
  pending: 'Ready to start',
  running: 'Playing',
  paused: 'Paused',
  completed: 'Completed',
  failed: 'Failed',
  aborted: 'Aborted',
}

const STATUS_COLORS: Record<ReplayStatus, string> = {
  pending: 'text-slate-400',
  running: 'text-blue-400',
  paused: 'text-yellow-400',
  completed: 'text-green-400',
  failed: 'text-red-400',
  aborted: 'text-slate-600',
}

export function ReplayMode() {
  const {
    investigation,
    replayState,
    events,
    startReplay,
    controlReplay,
  } = useInvestigationStore()

  const { session, isActive, currentStep, totalSteps, mode } = replayState
  const progress = totalSteps > 0 ? (currentStep / totalSteps) * 100 : 0
  const status = session?.status ?? 'pending'

  const canReplay = investigation?.replay_available === true

  return (
    <div className="flex flex-col h-full p-4 space-y-4">
      <div className="panel p-4">
        <div className="text-sm font-medium text-slate-200 mb-3">Replay Mode</div>
        <p className="text-xs text-slate-400 mb-4">
          Deterministic replay re-streams original investigation events. Does NOT re-execute agent code.
          All events are validated against the original hash chain before replay begins.
        </p>

        {/* Mode selection */}
        {!isActive && (
          <div className="space-y-3">
            <div className="text-xs text-slate-500 uppercase tracking-wider">Select Mode</div>
            <div className="grid grid-cols-3 gap-2">
              {[
                { mode: 'fast' as const, icon: <FastForward size={14} />, label: 'Fast (5x)', desc: '5x original speed' },
                { mode: 'live' as const, icon: <Play size={14} />, label: 'Live', desc: 'Original timing' },
                { mode: 'step' as const, icon: <SkipForward size={14} />, label: 'Step', desc: 'Manual advance' },
              ].map(({ mode: m, icon, label, desc }) => (
                <button
                  key={m}
                  onClick={() => startReplay(m, m === 'fast' ? 5 : 1)}
                  disabled={!canReplay}
                  className={clsx(
                    'panel p-3 text-center space-y-1 transition-colors',
                    canReplay ? 'hover:bg-slate-800 cursor-pointer' : 'opacity-40 cursor-not-allowed'
                  )}
                >
                  <div className="flex justify-center text-blue-400">{icon}</div>
                  <div className="text-xs font-medium text-slate-200">{label}</div>
                  <div className="text-[10px] text-slate-500">{desc}</div>
                </button>
              ))}
            </div>
            {!canReplay && (
              <div className="text-xs text-slate-500 text-center">
                Replay not available. Investigation must be completed first.
              </div>
            )}
          </div>
        )}

        {/* Active replay controls */}
        {isActive && (
          <div className="space-y-4">
            {/* Status */}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                {status === 'running' && <Zap size={14} className="text-blue-400 animate-pulse" />}
                <span className={clsx('text-sm font-medium', STATUS_COLORS[status])}>
                  {STATUS_LABELS[status]}
                </span>
                <span className="badge bg-slate-800 text-slate-400 text-[10px]">{mode}</span>
              </div>
              <span className="text-xs text-slate-400 font-mono">
                {currentStep} / {totalSteps}
              </span>
            </div>

            {/* Progress bar */}
            <div>
              <div className="h-2 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className={clsx(
                    'h-full rounded-full transition-all',
                    status === 'completed' ? 'bg-green-500' : status === 'failed' ? 'bg-red-500' : 'bg-blue-500'
                  )}
                  style={{ width: `${progress}%` }}
                />
              </div>
              <div className="flex justify-between mt-1 text-[10px] text-slate-500">
                <span>0</span>
                <span>{progress.toFixed(0)}%</span>
                <span>{totalSteps}</span>
              </div>
            </div>

            {/* Controls */}
            <div className="flex items-center gap-2">
              {status === 'running' && (
                <button
                  onClick={() => controlReplay('pause')}
                  className="btn-primary flex items-center gap-1.5"
                >
                  <Pause size={13} /> Pause
                </button>
              )}
              {status === 'paused' && (
                <button
                  onClick={() => controlReplay('resume')}
                  className="btn-primary flex items-center gap-1.5"
                >
                  <Play size={13} /> Resume
                </button>
              )}
              {mode === 'step' && (status === 'running' || status === 'paused') && (
                <button
                  onClick={() => controlReplay('step')}
                  className="btn flex items-center gap-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200"
                >
                  <SkipForward size={13} /> Next Step
                </button>
              )}
              {status !== 'completed' && status !== 'aborted' && (
                <button
                  onClick={() => controlReplay('abort')}
                  className="btn-danger flex items-center gap-1.5 ml-auto"
                >
                  <Square size={13} /> Abort
                </button>
              )}
              {(status === 'completed' || status === 'aborted') && (
                <button
                  onClick={() => startReplay(mode, mode === 'fast' ? 5 : 1)}
                  className="btn-primary flex items-center gap-1.5"
                >
                  <RotateCcw size={13} /> Replay Again
                </button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Live event stream (replayed events) */}
      <div className="panel flex-1 flex flex-col overflow-hidden">
        <div className="panel-header">
          <span className="text-xs font-medium text-slate-300">Replayed Events</span>
          <span className="badge bg-slate-800 text-slate-400">{currentStep}</span>
        </div>
        <div className="flex-1 scroll-panel p-2 space-y-1">
          {events
            .filter((e) => (e.payload as Record<string, unknown>)._replay === true)
            .slice(-50)
            .map((event) => (
              <div key={event.event_id} className="flex items-center gap-2 text-xs py-1 px-2 hover:bg-slate-800/50 rounded">
                <span className="font-mono text-slate-600 w-8 text-right">{event.sequence_num}</span>
                <span className="text-slate-300">{event.event_type}</span>
                <span className="text-slate-500 text-[10px] ml-auto">
                  {new Date(event.timestamp).toLocaleTimeString()}
                </span>
              </div>
            ))}
        </div>
      </div>

      {/* Validation info */}
      <div className="panel p-3 text-xs text-slate-500">
        <div className="flex items-center gap-1.5 mb-1">
          <span className="text-green-400">✓</span>
          <span>Hash chain validated before replay</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-green-400">✓</span>
          <span>Deterministic — same graph every time</span>
        </div>
      </div>
    </div>
  )
}

// Missing import
function RotateCcw({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
    </svg>
  )
}
