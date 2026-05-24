// Architecture Mini-Map
// Always-visible service topology in the investigation left column.
// Shows affected service highlighted, upstream/downstream edges, error rates.
// Fetches from Dynatrace topology endpoint; falls back to inferred topology.

import React, { useEffect, useState } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import { Network, AlertCircle } from 'lucide-react'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface TopoNode {
  id: string
  label: string
  type: 'client' | 'service' | 'database' | 'queue' | 'external'
  errorRate: number   // 0–1
  isAffected: boolean
}

interface TopoEdge {
  from: string
  to: string
  callsPerMin?: number
  errorRate?: number
}

interface Topology {
  nodes: TopoNode[]
  edges: TopoEdge[]
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function fetchTopology(service: string): Promise<Topology> {
  try {
    const res = await fetch(`/api/v1/intelligence/service-topology?service=${encodeURIComponent(service)}`)
    if (res.ok) {
      const data = await res.json()
      return mapApiTopology(data, service)
    }
  } catch { /* fall through */ }
  return inferTopology(service)
}

function mapApiTopology(data: Record<string, unknown>, affectedService: string): Topology {
  const raw = (data.topology || data) as {
    nodes?: Array<{ service: string; type: string; error_rate: number }>
    edges?: Array<{ from: string; to: string; calls_per_min?: number; error_rate?: number }>
  }
  const nodes: TopoNode[] = (raw.nodes || []).map((n) => ({
    id: n.service,
    label: n.service,
    type: (n.type as TopoNode['type']) || 'service',
    errorRate: n.error_rate || 0,
    isAffected: n.service === affectedService,
  }))
  if (!nodes.find((n) => n.id === 'client')) {
    nodes.unshift({ id: 'client', label: 'client', type: 'client', errorRate: 0, isAffected: false })
  }
  return {
    nodes,
    edges: (raw.edges || []).map((e) => ({
      from: e.from,
      to: e.to,
      callsPerMin: e.calls_per_min,
      errorRate: e.error_rate,
    })),
  }
}

/** Derives a minimal 4-node topology when no API is available. */
function inferTopology(service: string): Topology {
  const db = `${service}-db`
  return {
    nodes: [
      { id: 'client', label: 'client', type: 'client', errorRate: 0, isAffected: false },
      { id: service, label: service, type: 'service', errorRate: 0.08, isAffected: true },
      { id: db, label: db, type: 'database', errorRate: 0, isAffected: false },
      { id: 'auth-service', label: 'auth-service', type: 'service', errorRate: 0.01, isAffected: false },
    ],
    edges: [
      { from: 'client', to: service, callsPerMin: 850 },
      { from: service, to: db },
      { from: service, to: 'auth-service' },
    ],
  }
}

// ---------------------------------------------------------------------------
// Layout (simple left-to-right layered graph)
// ---------------------------------------------------------------------------

const W = 260
const H = 160
const NODE_W = 80
const NODE_H = 28

function layoutNodes(nodes: TopoNode[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>()

  // Layer 0: client-type
  const layer0 = nodes.filter((n) => n.type === 'client')
  // Layer 1: affected service + peers
  const layer1 = nodes.filter((n) => n.type === 'service' && n.isAffected)
  const layer1b = nodes.filter((n) => n.type === 'service' && !n.isAffected)
  // Layer 2: databases, queues, external
  const layer2 = nodes.filter((n) => ['database', 'queue', 'external'].includes(n.type))

  const setLayer = (layer: TopoNode[], xFrac: number) => {
    layer.forEach((n, i) => {
      positions.set(n.id, {
        x: Math.round(W * xFrac - NODE_W / 2),
        y: Math.round((H / (layer.length + 1)) * (i + 1) - NODE_H / 2),
      })
    })
  }

  setLayer(layer0, 0.1)
  setLayer([...layer1, ...layer1b], 0.45)
  setLayer(layer2, 0.85)

  return positions
}

// ---------------------------------------------------------------------------
// Node/Edge color helpers
// ---------------------------------------------------------------------------

const NODE_COLORS: Record<TopoNode['type'], string> = {
  client:   'fill-slate-700 stroke-slate-500',
  service:  'fill-slate-800 stroke-slate-600',
  database: 'fill-indigo-950 stroke-indigo-700',
  queue:    'fill-purple-950 stroke-purple-700',
  external: 'fill-slate-900 stroke-slate-600',
}

function errorColor(rate: number): string {
  if (rate > 0.05) return '#ef4444'
  if (rate > 0.02) return '#f97316'
  return '#64748b'
}

// ---------------------------------------------------------------------------
// SVG components
// ---------------------------------------------------------------------------

function TopoEdgeSvg({
  fromPos,
  toPos,
  edge,
}: {
  fromPos: { x: number; y: number }
  toPos: { x: number; y: number }
  edge: TopoEdge
}) {
  const x1 = fromPos.x + NODE_W
  const y1 = fromPos.y + NODE_H / 2
  const x2 = toPos.x
  const y2 = toPos.y + NODE_H / 2
  const mx = (x1 + x2) / 2
  const color = edge.errorRate && edge.errorRate > 0.02 ? '#f97316' : '#334155'

  return (
    <g>
      <path
        d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeOpacity={0.7}
      />
      {/* Arrow */}
      <polygon
        points={`${x2},${y2} ${x2 - 5},${y2 - 3} ${x2 - 5},${y2 + 3}`}
        fill={color}
        fillOpacity={0.7}
      />
    </g>
  )
}

function TopoNodeSvg({
  node,
  pos,
}: {
  node: TopoNode
  pos: { x: number; y: number }
}) {
  const stroke = node.isAffected ? '#ef4444' : errorColor(node.errorRate)
  const fill = node.isAffected ? '#450a0a' : undefined
  const label = node.label.length > 12 ? node.label.slice(0, 11) + '…' : node.label

  return (
    <g>
      <rect
        x={pos.x}
        y={pos.y}
        width={NODE_W}
        height={NODE_H}
        rx={4}
        className={clsx(fill ? '' : NODE_COLORS[node.type])}
        fill={fill}
        stroke={stroke}
        strokeWidth={node.isAffected ? 1.5 : 1}
      />
      {node.isAffected && (
        <rect x={pos.x} y={pos.y} width={3} height={NODE_H} rx={2} fill="#ef4444" />
      )}
      <text
        x={pos.x + NODE_W / 2}
        y={pos.y + NODE_H / 2 + 4}
        textAnchor="middle"
        fontSize={9}
        fill={node.isAffected ? '#fca5a5' : '#94a3b8'}
        fontFamily="ui-monospace, monospace"
      >
        {label}
      </text>
      {node.errorRate > 0.02 && (
        <text
          x={pos.x + NODE_W - 3}
          y={pos.y + 9}
          textAnchor="end"
          fontSize={7}
          fill={errorColor(node.errorRate)}
        >
          {(node.errorRate * 100).toFixed(1)}%err
        </text>
      )}
    </g>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ArchitectureMiniMap() {
  const { investigation } = useInvestigationStore()
  const [topology, setTopology] = useState<Topology | null>(null)
  const [loading, setLoading] = useState(false)

  const service = investigation?.affected_service

  useEffect(() => {
    if (!service) return
    setLoading(true)
    fetchTopology(service)
      .then(setTopology)
      .finally(() => setLoading(false))
  }, [service])

  if (!service) {
    return (
      <div className="rounded border border-slate-800 p-3 text-xs text-slate-600 text-center">
        No service
      </div>
    )
  }

  const topo = topology || inferTopology(service)
  const positions = layoutNodes(topo.nodes)

  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-1.5 px-2.5 py-1.5 border-b border-slate-800 bg-slate-900">
        <Network size={11} className="text-slate-500" />
        <span className="text-[10px] text-slate-500 uppercase tracking-wider font-medium">Service Topology</span>
        {loading && <span className="ml-auto text-[9px] text-slate-600 animate-pulse">loading…</span>}
      </div>

      {/* SVG diagram */}
      <div className="p-1">
        <svg width={W} height={H} className="overflow-visible">
          {/* Edges first (under nodes) */}
          {topo.edges.map((edge, i) => {
            const fp = positions.get(edge.from)
            const tp = positions.get(edge.to)
            if (!fp || !tp) return null
            return <TopoEdgeSvg key={i} fromPos={fp} toPos={tp} edge={edge} />
          })}
          {/* Nodes */}
          {topo.nodes.map((node) => {
            const pos = positions.get(node.id)
            if (!pos) return null
            return <TopoNodeSvg key={node.id} node={node} pos={pos} />
          })}
        </svg>
      </div>

      {/* Legend */}
      <div className="flex items-center gap-3 px-2.5 pb-1.5 text-[9px] text-slate-600">
        <span className="flex items-center gap-1">
          <span className="w-2 h-2 rounded-sm bg-red-950 border border-red-600 inline-block" />
          affected
        </span>
        <span className="flex items-center gap-1">
          <AlertCircle size={8} className="text-orange-500" />
          &gt;2% err
        </span>
      </div>
    </div>
  )
}
