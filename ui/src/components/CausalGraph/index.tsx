import {
  ReactFlow,
  Background, Controls, MiniMap,
  useNodesState, useEdgesState,
  Handle, Position,
  BackgroundVariant, MarkerType,
  Node, Edge, NodeProps,
  NodeTypes
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useCallback, useState } from 'react'
import { X, AlertTriangle } from 'lucide-react'

type ServiceNodeData = {
  label: string
  health: number
  tier: number
  alertCount: number
  technologies: string[]
  isOrigin?: boolean
  dimmed?: boolean
  blastHighlight?: boolean
}

type ServiceNode = Node<ServiceNodeData>

function healthColor(health: unknown): string {
  const h = typeof health === 'number' ? health : 1
  if (h > 0.9) return '#10b981'
  if (h > 0.7) return '#f59e0b'
  return '#ef4444'
}

function healthBorderClass(health: number): string {
  if (health > 0.9) return 'border-emerald-500/60'
  if (health > 0.7) return 'border-yellow-500/60'
  return 'border-red-500/70'
}

function ServiceNodeComponent({ data }: NodeProps<ServiceNode>) {
  const h = data.health ?? 1
  const isLarge = data.tier === 1
  const w = isLarge ? 150 : 140
  const height = isLarge ? 62 : 56
  const color = healthColor(h)
  const borderCls = healthBorderClass(h)
  const originCls = data.isOrigin ? 'ring-2 ring-red-500/60 animate-pulse' : ''
  const dimmedStyle = data.dimmed ? { opacity: 0.3 } : {}
  const blastCls = data.blastHighlight ? 'ring-2 ring-red-500 animate-pulse' : ''

  return (
    <div
      style={{ width: w, height, ...dimmedStyle, background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 100%)' }}
      className={`border rounded-md relative overflow-hidden select-none ${borderCls} ${originCls} ${blastCls}`}
    >
      <Handle type="target" position={Position.Top} className="opacity-0" />
      <div style={{ width: 4, position: 'absolute', left: 0, top: 0, bottom: 0, background: color, opacity: 0.9 }} />
      <div className="pl-3 pr-2 pt-1.5 pb-1">
        <div className="text-white font-semibold truncate" style={{ fontSize: 11 }}>{data.label}</div>
        <div style={{ fontSize: 10, color }}>{Math.round(h * 100)}% health</div>
      </div>
      {(data.alertCount ?? 0) > 0 && (
        <div className="absolute top-1 right-1 bg-red-600 text-white rounded-full flex items-center justify-center font-bold" style={{ fontSize: 9, minWidth: 16, height: 16, padding: '0 3px' }}>
          {data.alertCount}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="opacity-0" />
    </div>
  )
}

const nodeTypes: NodeTypes = { serviceNode: ServiceNodeComponent }

function makeNode(id: string, label: string, x: number, y: number, tier: number, health: number, alertCount = 0, techs: string[] = [], isOrigin = false): ServiceNode {
  return {
    id,
    type: 'serviceNode',
    position: { x, y },
    data: { label, health, tier, alertCount, technologies: techs, isOrigin },
  }
}

const DEMO_NODES: ServiceNode[] = [
  // Tier 1 — top row
  makeNode('api-gateway', 'api-gateway', 300, 0, 1, 0.95, 0, ['nginx', 'lua']),
  makeNode('cdn-edge', 'cdn-edge', 700, 0, 1, 0.98, 0, ['cloudfront']),
  // Tier 2 — middle
  makeNode('auth-service', 'auth-service', 0, 160, 2, 0.88, 1, ['node', 'jwt']),
  makeNode('payment-service', 'payment-service', 200, 160, 2, 0.62, 3, ['java', 'stripe'], true),
  makeNode('order-service', 'order-service', 400, 160, 2, 0.75, 2, ['python']),
  makeNode('cart-service', 'cart-service', 600, 160, 2, 0.91, 0, ['go']),
  makeNode('search-service', 'search-service', 800, 160, 2, 0.82, 1, ['elasticsearch']),
  // Tier 3 — bottom
  makeNode('postgres-primary', 'postgres-primary', 0, 320, 3, 0.55, 4, ['postgres']),
  makeNode('redis-cluster', 'redis-cluster', 175, 320, 3, 0.93, 0, ['redis']),
  makeNode('kafka-broker', 'kafka-broker', 350, 320, 3, 0.88, 0, ['kafka']),
  makeNode('elasticsearch-cluster', 'elasticsearch-cluster', 550, 320, 3, 0.78, 1, ['es']),
  makeNode('mobile-bff', 'mobile-bff', 740, 320, 3, 0.96, 0, ['graphql']),
  makeNode('recommendation-engine', 'recommendation-engine', 950, 320, 3, 0.85, 0, ['python', 'ml']),
  makeNode('inventory-service', 'inventory-service', 50, 480, 3, 0.71, 2, ['java']),
  makeNode('notification-service', 'notification-service', 280, 480, 3, 0.94, 0, ['node']),
  makeNode('user-service', 'user-service', 510, 480, 3, 0.89, 0, ['go']),
]

function makeEdge(id: string, source: string, target: string, corr: number, vol: number, type = 'http'): Edge {
  let stroke = '#3b82f6'
  if (corr > 0.8) stroke = '#ef4444'
  else if (corr > 0.6) stroke = '#f59e0b'
  const sw = 1.5 + (vol / 1000) * 1.5
  return {
    id,
    source,
    target,
    animated: true,
    label: type,
    labelStyle: { fontSize: 8, fill: '#64748b' },
    style: { stroke, strokeWidth: Math.min(sw, 3) },
    markerEnd: { type: MarkerType.ArrowClosed, color: stroke },
  }
}

const DEMO_EDGES: Edge[] = [
  makeEdge('e1', 'api-gateway', 'auth-service', 0.5, 800),
  makeEdge('e2', 'api-gateway', 'order-service', 0.65, 600, 'http'),
  makeEdge('e3', 'api-gateway', 'cart-service', 0.4, 500),
  makeEdge('e4', 'cdn-edge', 'api-gateway', 0.3, 1200),
  makeEdge('e5', 'cdn-edge', 'search-service', 0.5, 400),
  makeEdge('e6', 'order-service', 'payment-service', 0.85, 300, 'grpc'),
  makeEdge('e7', 'order-service', 'inventory-service', 0.7, 200),
  makeEdge('e8', 'payment-service', 'postgres-primary', 0.9, 250, 'sql'),
  makeEdge('e9', 'auth-service', 'redis-cluster', 0.45, 900, 'cache'),
  makeEdge('e10', 'order-service', 'kafka-broker', 0.55, 150, 'event'),
  makeEdge('e11', 'kafka-broker', 'notification-service', 0.4, 100),
  makeEdge('e12', 'search-service', 'elasticsearch-cluster', 0.6, 350),
  makeEdge('e13', 'cart-service', 'redis-cluster', 0.35, 700, 'cache'),
  makeEdge('e14', 'api-gateway', 'mobile-bff', 0.3, 200),
  makeEdge('e15', 'recommendation-engine', 'user-service', 0.4, 100),
  makeEdge('e16', 'order-service', 'user-service', 0.5, 180),
]

interface SelectedNodePanelProps {
  node: ServiceNode
  onClose: () => void
  onBlastRadius: (id: string) => void
}

function SelectedNodePanel({ node, onClose, onBlastRadius }: SelectedNodePanelProps) {
  const h = node.data.health ?? 1
  const color = healthColor(h)
  const pct = Math.round(h * 100)
  // SVG arc gauge
  const r = 28, cx = 36, cy = 36
  const circ = 2 * Math.PI * r
  const dash = (pct / 100) * circ * 0.75
  const offset = circ * 0.125

  return (
    <div className="absolute right-0 top-0 bottom-0 w-64 bg-slate-900/95 backdrop-blur border-l border-slate-800 z-10 flex flex-col shadow-2xl">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
        <span className="text-slate-100 font-semibold text-sm truncate">{node.data.label}</span>
        <button onClick={onClose} className="text-slate-500 hover:text-slate-200 transition"><X size={14}/></button>
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
        {/* Health gauge */}
        <div className="flex items-center justify-center">
          <svg width={72} height={72}>
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="#1e293b" strokeWidth={6} strokeDasharray={`${circ * 0.75} ${circ * 0.25}`} strokeDashoffset={offset} strokeLinecap="round" transform={`rotate(135 ${cx} ${cy})`} />
            <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth={6} strokeDasharray={`${dash} ${circ - dash}`} strokeDashoffset={offset} strokeLinecap="round" transform={`rotate(135 ${cx} ${cy})`} />
            <text x={cx} y={cy + 5} textAnchor="middle" fill={color} fontSize={14} fontWeight="600">{pct}%</text>
          </svg>
        </div>
        {/* Alert count */}
        {node.data.alertCount > 0 && (
          <div className="flex items-center gap-2 bg-red-950/40 border border-red-800/50 rounded-md px-3 py-2">
            <AlertTriangle size={14} className="text-red-400 shrink-0"/>
            <span className="text-red-300 text-xs">{node.data.alertCount} active alert{node.data.alertCount > 1 ? 's':''}</span>
          </div>
        )}
        {/* Technologies */}
        {node.data.technologies?.length > 0 && (
          <div>
            <div className="text-slate-500 text-xs mb-1.5">Technologies</div>
            <div className="flex flex-wrap gap-1.5">
              {node.data.technologies.map(t => (
                <span key={t} className="text-[10px] px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 border border-slate-700">{t}</span>
              ))}
            </div>
          </div>
        )}
        {/* Tier */}
        <div className="text-slate-500 text-xs">Tier <span className="text-slate-300">{node.data.tier}</span></div>
      </div>
      <div className="px-4 py-3 border-t border-slate-800">
        <button
          onClick={() => onBlastRadius(node.id)}
          className="w-full text-xs px-3 py-2 rounded-md bg-red-950/50 border border-red-800/60 text-red-300 hover:bg-red-900/50 transition"
        >
          View Blast Radius
        </button>
      </div>
    </div>
  )
}

export function CausalGraph({ className }: { className?: string }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<ServiceNode>(DEMO_NODES)
  const [edges, setEdges, onEdgesChange] = useEdgesState(DEMO_EDGES)
  const [selectedNode, setSelectedNode] = useState<ServiceNode | null>(null)
  const [blastBanner, setBlastBanner] = useState<string | null>(null)

  // Fetch topology on mount and every 30s
  useEffect(() => {
    const fetchData = async () => {
      try {
        const [topoRes, healthRes] = await Promise.all([
          fetch('/api/graph/topology'),
          fetch('/api/graph/health-summary'),
        ])
        if (!topoRes.ok || !healthRes.ok) return
        // If API works, transform data; otherwise stay on demo
      } catch {
        // silently use demo data
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [])

  const handleNodeClick = useCallback((_: React.MouseEvent, node: ServiceNode) => {
    setSelectedNode(node)
  }, [])

  const handlePaneClick = useCallback(() => {
    setSelectedNode(null)
    // clear blast radius
    setNodes(nds => nds.map(n => ({ ...n, data: { ...n.data, dimmed: false, blastHighlight: false } })))
    setEdges(eds => eds.map(e => ({ ...e, style: DEMO_EDGES.find(d => d.id === e.id)?.style ?? e.style, animated: true })))
    setBlastBanner(null)
  }, [setNodes, setEdges])

  const handleBlastRadius = useCallback(async (serviceId: string) => {
    let affected: string[] = []
    try {
      const res = await fetch(`/api/graph/blast-radius/${serviceId}`)
      if (res.ok) {
        const data: unknown = await res.json()
        if (data && typeof data === 'object' && 'affected_services' in data) {
          const d = data as { affected_services: unknown }
          if (Array.isArray(d.affected_services)) {
            affected = d.affected_services.filter((x): x is string => typeof x === 'string')
          }
        }
      }
    } catch {
      // fallback: compute from edges
      const outEdges = DEMO_EDGES.filter(e => e.source === serviceId)
      affected = outEdges.map(e => e.target)
    }

    setNodes(nds => nds.map(n => ({
      ...n,
      data: {
        ...n.data,
        dimmed: n.id !== serviceId && !affected.includes(n.id),
        blastHighlight: affected.includes(n.id),
        isOrigin: n.id === serviceId,
      }
    })))
    setEdges(eds => eds.map(e => {
      const inPath = e.source === serviceId || affected.includes(e.source)
      return {
        ...e,
        style: { ...e.style, stroke: inPath ? '#ef4444' : '#334155', strokeWidth: inPath ? 2.5 : 1 },
        animated: inPath,
      }
    }))
    setBlastBanner(`Blast Radius: ${serviceId} → ${affected.length} service${affected.length !== 1 ? 's':''} affected`)
  }, [setNodes, setEdges])

  return (
    <div className={`h-full w-full bg-slate-950 relative ${className ?? ''}`}>
      {blastBanner && (
        <div className="absolute top-2 left-1/2 -translate-x-1/2 z-20 bg-red-950/90 border border-red-700/60 text-red-200 text-xs px-4 py-1.5 rounded-full shadow-lg">
          {blastBanner}
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onPaneClick={handlePaneClick}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={2}
        attributionPosition="bottom-left"
      >
        <Background variant={BackgroundVariant.Dots} gap={24} size={1} color="#1e293b" />
        <Controls className="!bg-slate-800 !border-slate-700 !text-slate-300" />
        <MiniMap className="!bg-slate-900 !border-slate-700" nodeColor={(n) => healthColor((n.data as ServiceNodeData).health)} />
      </ReactFlow>
      {selectedNode && (
        <SelectedNodePanel
          node={selectedNode}
          onClose={() => setSelectedNode(null)}
          onBlastRadius={handleBlastRadius}
        />
      )}
    </div>
  )
}
