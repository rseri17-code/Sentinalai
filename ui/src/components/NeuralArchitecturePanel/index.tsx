/**
 * Dynamic Architecture Panel
 *
 * Live view of SentinalAI's operational cognition architecture:
 *   - Neural Quality Net  (9 → 16 → 8 → 1)
 *   - Confidence Calibrator (5 → 16 → 1)
 *
 * Polls /api/v1/intelligence/neural-architecture every 30s.
 * Shows layer topology, weight-norm heatmap, training state, and blend progress.
 */

import React, { useEffect, useState, useCallback } from 'react'
import { RefreshCw, Cpu, Zap, AlertCircle, CheckCircle2, Clock } from 'lucide-react'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ModelArch {
  name: string
  description: string
  layer_sizes: number[]
  weight_norms: number[]
  bias_norms: number[]
  features: string[]
  total_samples: number
  blend_alpha: number
  active: boolean
  fully_blended?: boolean
  lr: number
  momentum: number
  grad_clip: number
  max_blend_weight: number
  min_samples_for_blend: number
  error?: string
}

interface ArchData {
  quality_net: ModelArch
  confidence_calibrator: ModelArch
}

const BASE = (import.meta as any).env?.VITE_API_BASE ?? ''

async function fetchArch(): Promise<ArchData> {
  const res = await fetch(`${BASE}/api/v1/intelligence/neural-architecture`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

// ---------------------------------------------------------------------------
// SVG Network Diagram
// ---------------------------------------------------------------------------

const LAYER_LABELS = ['input', 'h₁', 'h₂', 'h₃', 'out']

function NetworkDiagram({ model }: { model: ModelArch }) {
  const sizes = model.layer_sizes
  const norms = model.weight_norms
  const maxNorm = Math.max(...norms, 0.001)

  const W = 520
  const H = 160
  const padX = 48
  const layerW = (W - padX * 2) / (sizes.length - 1)

  const xs = sizes.map((_, i) => padX + i * layerW)

  // Colour by norm fraction: cool blue → warm amber
  const normColor = (idx: number) => {
    const t = norms[idx] / maxNorm
    const r = Math.round(59 + t * (251 - 59))
    const g = Math.round(130 + t * (146 - 130))
    const b = Math.round(246 + t * (0 - 246))
    return `rgb(${r},${g},${b})`
  }

  // Node height proportional to layer size, capped for display
  const maxSize = Math.max(...sizes)
  const nodeH = (size: number) => Math.max(28, Math.min(120, (size / maxSize) * 120))

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${W} ${H}`}
      className="overflow-visible"
      aria-label={`${model.name} architecture diagram`}
    >
      {/* Connection bands between layers */}
      {sizes.slice(0, -1).map((_, li) => {
        const x1 = xs[li]
        const x2 = xs[li + 1]
        const h1 = nodeH(sizes[li])
        const h2 = nodeH(sizes[li + 1])
        const cy = H / 2
        const color = normColor(li)
        const strokeW = Math.max(3, (norms[li] / maxNorm) * 14)
        return (
          <g key={li}>
            <line
              x1={x1} y1={cy - h1 / 2}
              x2={x2} y2={cy - h2 / 2}
              stroke={color} strokeWidth={strokeW * 0.4}
              strokeOpacity={0.5}
            />
            <line
              x1={x1} y1={cy + h1 / 2}
              x2={x2} y2={cy + h2 / 2}
              stroke={color} strokeWidth={strokeW * 0.4}
              strokeOpacity={0.5}
            />
            <line
              x1={x1} y1={cy}
              x2={x2} y2={cy}
              stroke={color} strokeWidth={strokeW}
              strokeOpacity={0.8}
              strokeLinecap="round"
            />
            {/* Norm label */}
            <text
              x={(x1 + x2) / 2}
              y={cy - Math.max(h1, h2) / 2 - 6}
              textAnchor="middle"
              fontSize={9}
              fill="#64748b"
            >
              ‖W‖={norms[li].toFixed(2)}
            </text>
          </g>
        )
      })}

      {/* Layer nodes */}
      {sizes.map((size, li) => {
        const cx = xs[li]
        const cy = H / 2
        const h = nodeH(size)
        const isInput = li === 0
        const isOutput = li === sizes.length - 1
        const fill = isOutput ? '#1d4ed8' : isInput ? '#1e3a5f' : '#1e293b'
        const stroke = isOutput ? '#3b82f6' : isInput ? '#3b82f6' : '#475569'
        const label = LAYER_LABELS[li] ?? `h${li}`

        return (
          <g key={li}>
            <rect
              x={cx - 16}
              y={cy - h / 2}
              width={32}
              height={h}
              rx={4}
              fill={fill}
              stroke={stroke}
              strokeWidth={1.5}
            />
            {/* Node count */}
            <text
              x={cx}
              y={cy + 4}
              textAnchor="middle"
              fontSize={11}
              fontWeight="600"
              fill="#e2e8f0"
            >
              {size}
            </text>
            {/* Layer label */}
            <text
              x={cx}
              y={H - 2}
              textAnchor="middle"
              fontSize={9}
              fill="#94a3b8"
            >
              {label}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Blend Progress Bar
// ---------------------------------------------------------------------------

function BlendBar({ alpha, max }: { alpha: number; max: number }) {
  const pct = max > 0 ? (alpha / max) * 100 : 0
  return (
    <div>
      <div className="flex justify-between text-xs text-slate-400 mb-1">
        <span>Neural blend</span>
        <span className="font-mono">{(alpha * 100).toFixed(1)}% / {(max * 100).toFixed(0)}% max</span>
      </div>
      <div className="h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{
            width: `${pct}%`,
            background: pct > 80 ? '#22c55e' : pct > 40 ? '#3b82f6' : '#f59e0b',
          }}
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------------

function StatusBadge({ model }: { model: ModelArch }) {
  if (model.error) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-red-900/40 text-red-400 border border-red-800">
        <AlertCircle size={10} /> Error
      </span>
    )
  }
  if (model.fully_blended) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-green-900/40 text-green-400 border border-green-800">
        <CheckCircle2 size={10} /> Fully blended
      </span>
    )
  }
  if (model.active) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-blue-900/40 text-blue-400 border border-blue-800">
        <Zap size={10} /> Blending
      </span>
    )
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-slate-800 text-slate-500 border border-slate-700">
      <Clock size={10} /> Warming up
    </span>
  )
}

// ---------------------------------------------------------------------------
// Model Card
// ---------------------------------------------------------------------------

function ModelCard({ model }: { model: ModelArch }) {
  if (model.error) {
    return (
      <div className="panel p-5 flex-1">
        <div className="flex items-center gap-2 mb-2 text-red-400">
          <AlertCircle size={16} />
          <span className="font-medium">{model.name ?? 'Model'}</span>
        </div>
        <p className="text-sm text-slate-500">{model.error}</p>
      </div>
    )
  }

  const archStr = model.layer_sizes.join(' → ')

  return (
    <div className="panel p-5 flex-1 min-w-0">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-4">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Cpu size={14} className="text-blue-400 flex-shrink-0" />
            <span className="text-sm font-semibold text-slate-100">{model.name}</span>
          </div>
          <p className="text-xs text-slate-400 leading-relaxed">{model.description}</p>
        </div>
        <StatusBadge model={model} />
      </div>

      {/* Architecture string + diagram */}
      <div className="mb-3">
        <div className="text-xs font-mono text-blue-400 mb-2">{archStr}</div>
        <div className="bg-slate-950/60 rounded-lg px-2 py-3">
          <NetworkDiagram model={model} />
        </div>
      </div>

      {/* Blend bar */}
      <div className="mb-4">
        <BlendBar alpha={model.blend_alpha} max={model.max_blend_weight} />
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-3 gap-2 mb-4">
        {[
          { label: 'Samples', value: model.total_samples.toLocaleString() },
          { label: 'LR', value: model.lr },
          { label: 'Grad clip', value: model.grad_clip },
        ].map(({ label, value }) => (
          <div key={label} className="bg-slate-800/60 rounded p-2 text-center">
            <div className="text-xs text-slate-500 mb-0.5">{label}</div>
            <div className="text-sm font-mono text-slate-200">{value}</div>
          </div>
        ))}
      </div>

      {/* Input features */}
      <div>
        <div className="text-xs text-slate-500 uppercase tracking-wider mb-2">Input features</div>
        <div className="flex flex-wrap gap-1">
          {model.features.map((f, i) => (
            <span
              key={f}
              className="px-1.5 py-0.5 rounded text-xs bg-slate-800 text-slate-400 font-mono"
            >
              {i}: {f}
            </span>
          ))}
        </div>
      </div>

      {/* Weight norms per layer */}
      {model.weight_norms?.length > 0 && (
        <div className="mt-4">
          <div className="text-xs text-slate-500 uppercase tracking-wider mb-2">Layer weight norms</div>
          <div className="space-y-1">
            {model.weight_norms.map((norm, i) => {
              const max = Math.max(...model.weight_norms, 0.001)
              const pct = (norm / max) * 100
              return (
                <div key={i} className="flex items-center gap-2 text-xs">
                  <span className="text-slate-500 font-mono w-16 flex-shrink-0">
                    L{i}→L{i + 1}
                  </span>
                  <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${pct}%`,
                        background: `hsl(${210 + pct}, 70%, 55%)`,
                      }}
                    />
                  </div>
                  <span className="text-slate-400 font-mono w-12 text-right">{norm.toFixed(3)}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// System Cognition Overview
// ---------------------------------------------------------------------------

function CognitionOverview() {
  return (
    <div className="panel p-5 mb-6">
      <h2 className="text-sm font-semibold text-slate-100 mb-3">Operational Cognition Stack</h2>
      <div className="grid grid-cols-4 gap-3">
        {[
          {
            layer: '① Grounding',
            desc: 'Deterministic telemetry retrieval, topology scoping, evidence ranking',
            color: 'border-l-green-500',
          },
          {
            layer: '② Decision',
            desc: 'Hypothesis ranking, evidence weighting, confidence scoring, receipt generation',
            color: 'border-l-blue-500',
          },
          {
            layer: '③ Neural Overlay',
            desc: 'Quality net + confidence calibrator blend learned signal onto deterministic base',
            color: 'border-l-violet-500',
          },
          {
            layer: '④ Pattern Intelligence',
            desc: 'Always-on background loop — precursor detection, SLO burn, cross-service correlation',
            color: 'border-l-amber-500',
          },
        ].map(({ layer, desc, color }) => (
          <div
            key={layer}
            className={clsx('bg-slate-800/50 rounded-lg p-3 border-l-2', color)}
          >
            <div className="text-xs font-semibold text-slate-300 mb-1">{layer}</div>
            <div className="text-xs text-slate-500 leading-relaxed">{desc}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Panel
// ---------------------------------------------------------------------------

export function NeuralArchitecturePanel() {
  const [data, setData] = useState<ArchData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await fetchArch()
      setData(d)
      setLastRefresh(new Date())
    } catch (e: any) {
      setError(e.message ?? 'Failed to fetch architecture data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 30_000)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <div className="h-full overflow-y-auto p-6 space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">Dynamic Architecture</h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Live view of SentinalAI's self-evolving operational cognition models
          </p>
        </div>
        <div className="flex items-center gap-3">
          {lastRefresh && (
            <span className="text-xs text-slate-500">
              Updated {lastRefresh.toLocaleTimeString()}
            </span>
          )}
          <button
            onClick={refresh}
            disabled={loading}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs transition-colors disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </div>

      {/* Cognition stack overview */}
      <CognitionOverview />

      {/* Error state */}
      {error && !data && (
        <div className="panel p-6 text-center text-slate-500">
          <AlertCircle size={24} className="mx-auto mb-2 text-red-500" />
          <p className="text-sm text-red-400">{error}</p>
          <button
            onClick={refresh}
            className="mt-3 text-xs text-slate-400 underline"
          >
            Try again
          </button>
        </div>
      )}

      {/* Skeleton while loading with no data yet */}
      {loading && !data && (
        <div className="flex gap-4">
          {[0, 1].map((i) => (
            <div key={i} className="flex-1 panel p-5 animate-pulse space-y-4">
              <div className="h-4 bg-slate-800 rounded w-40" />
              <div className="h-3 bg-slate-800 rounded w-full" />
              <div className="h-32 bg-slate-800 rounded" />
              <div className="h-2 bg-slate-800 rounded" />
            </div>
          ))}
        </div>
      )}

      {/* Neural model cards */}
      {data && (
        <div className="flex gap-4">
          <ModelCard model={data.quality_net} />
          <ModelCard model={data.confidence_calibrator} />
        </div>
      )}

      {/* Architecture principles */}
      <div className="panel p-5">
        <h2 className="text-sm font-semibold text-slate-100 mb-3">Architecture Principles</h2>
        <div className="grid grid-cols-2 gap-4 text-xs text-slate-400 leading-relaxed">
          <div className="space-y-2">
            <div>
              <span className="text-slate-300 font-medium">Deterministic first.</span>{' '}
              95% of every investigation result is grounded in deterministic telemetry retrieval.
              Neural models add a learned overlay — never replace the evidence-backed base.
            </div>
            <div>
              <span className="text-slate-300 font-medium">Blend ramp-up.</span>{' '}
              Neural weight starts at 0, grows linearly to the configured maximum as training
              samples accumulate. This prevents an undertrained model from polluting results.
            </div>
          </div>
          <div className="space-y-2">
            <div>
              <span className="text-slate-300 font-medium">Heavy-ball SGD momentum.</span>{' '}
              Standard momentum accumulates velocity for stable convergence online. One gradient
              step per completed investigation — no batch accumulation, no external dependencies.
            </div>
            <div>
              <span className="text-slate-300 font-medium">Replayable by design.</span>{' '}
              All model checkpoints are JSON-serializable. Any investigation can be re-run with
              the exact model state that produced the original result.
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
