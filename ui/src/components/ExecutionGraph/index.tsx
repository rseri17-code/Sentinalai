// Panel 2: Execution Graph (DAG Visualization)
// Uses @xyflow/react for interactive DAG rendering

import React, { useCallback, useEffect, useMemo } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  MarkerType,
  BackgroundVariant,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useInvestigationStore } from '@/store/investigationStore'
import type { GraphNode, NodeStatus } from '@/types'
import clsx from 'clsx'

// Node status → color mapping
const NODE_COLORS: Record<NodeStatus, { bg: string; border: string; text: string }> = {
  pending: { bg: '#1e293b', border: '#475569', text: '#94a3b8' },
  running: { bg: '#1e3a5f', border: '#3b82f6', text: '#93c5fd' },
  success: { bg: '#14532d', border: '#22c55e', text: '#86efac' },
  failed: { bg: '#450a0a', border: '#ef4444', text: '#fca5a5' },
  timeout: { bg: '#431407', border: '#f97316', text: '#fdba74' },
  awaiting_approval: { bg: '#2e1065', border: '#a855f7', text: '#d8b4fe' },
  approved: { bg: '#14532d', border: '#22c55e', text: '#86efac' },
  rejected: { bg: '#450a0a', border: '#ef4444', text: '#fca5a5' },
  skipped: { bg: '#1e293b', border: '#334155', text: '#64748b' },
}

const NODE_TYPE_ICONS: Record<string, string> = {
  investigation: '🔎',
  classification: '🏷',
  playbook: '📖',
  tool_call: '⚙️',
  llm_inference: '🤖',
  hypothesis: '🧠',
  memory_query: '🧬',
  decision: '🎯',
  control: '🔐',
  rca: '📋',
}

function CustomNode({ data }: { data: { node: GraphNode } }) {
  const { node } = data
  const colors = NODE_COLORS[node.status] || NODE_COLORS.pending
  const icon = NODE_TYPE_ICONS[node.node_type] || '•'
  const isRunning = node.status === 'running'

  return (
    <div
      className={clsx('rounded px-3 py-2 text-xs min-w-[100px] max-w-[160px]', isRunning && 'node-running')}
      style={{
        background: colors.bg,
        border: `1.5px solid ${colors.border}`,
        color: colors.text,
      }}
    >
      <div className="flex items-center gap-1.5 mb-0.5">
        <span className="text-sm">{icon}</span>
        <span className="font-medium text-slate-200 text-[11px] truncate">{node.label.split('\n')[0]}</span>
      </div>
      {node.label.includes('\n') && (
        <div className="text-[10px] truncate opacity-70">{node.label.split('\n').slice(1).join(' ')}</div>
      )}
      {node.duration_ms !== undefined && node.duration_ms !== null && (
        <div className="mt-1 text-[10px] opacity-60">{node.duration_ms.toFixed(0)}ms</div>
      )}
      {node.confidence !== undefined && (
        <div className="mt-1 text-[10px]" style={{ color: colors.border }}>
          {(node.confidence * 100).toFixed(0)}%
        </div>
      )}
      {node.error_message && (
        <div className="mt-1 text-[10px] text-red-400 truncate">{node.error_message}</div>
      )}
    </div>
  )
}

const nodeTypes = { custom: CustomNode }

function convertToFlowNodes(graphNodes: GraphNode[]): Node[] {
  return graphNodes.map((node) => ({
    id: node.node_id,
    type: 'custom',
    position: { x: node.x ?? 0, y: node.y ?? 0 },
    data: { node },
    selected: false,
  }))
}

function convertToFlowEdges(edges: import('@/types').GraphEdge[]): Edge[] {
  return edges.map((edge) => ({
    id: edge.edge_id,
    source: edge.source_id,
    target: edge.target_id,
    label: edge.label,
    type: 'smoothstep',
    markerEnd: { type: MarkerType.ArrowClosed, color: '#475569' },
    style: { stroke: '#475569', strokeWidth: 1.5 },
  }))
}

function NodeDetailPanel({ nodeId }: { nodeId: string }) {
  const { graph, receipts } = useInvestigationStore()
  const node = graph?.nodes.find((n) => n.node_id === nodeId)
  if (!node) return null

  const receipt = node.receipt_id
    ? receipts.find((r) => r.receipt_id === node.receipt_id)
    : null

  return (
    <div className="w-72 panel overflow-y-auto">
      <div className="panel-header">
        <span className="text-sm font-medium text-slate-200">Node Detail</span>
      </div>
      <div className="p-3 space-y-2 text-xs">
        <Row label="Type" value={node.node_type} />
        <Row label="Status" value={node.status} />
        {node.worker && <Row label="Worker" value={node.worker} />}
        {node.action && <Row label="Action" value={node.action} />}
        {node.started_at && <Row label="Started" value={new Date(node.started_at).toLocaleTimeString()} />}
        {node.completed_at && <Row label="Completed" value={new Date(node.completed_at).toLocaleTimeString()} />}
        {node.duration_ms !== undefined && <Row label="Duration" value={`${node.duration_ms?.toFixed(1)}ms`} />}
        {node.confidence !== undefined && <Row label="Confidence" value={`${((node.confidence ?? 0) * 100).toFixed(0)}%`} />}
        {node.error_message && (
          <div>
            <div className="text-slate-500 mb-0.5">Error</div>
            <div className="text-red-400">{node.error_message}</div>
          </div>
        )}
        {node.trace_id && (
          <div>
            <div className="text-slate-500 mb-0.5">Trace ID</div>
            <div className="font-mono text-slate-400 text-[10px] break-all">{node.trace_id}</div>
          </div>
        )}

        {/* Receipt summary */}
        {receipt && (
          <div className="mt-3 pt-3 border-t border-slate-800">
            <div className="text-slate-500 mb-1 uppercase tracking-wider text-[10px]">Evidence Receipt</div>
            <Row label="Status" value={receipt.status} />
            <Row label="Tool" value={receipt.tool} />
            <Row label="Elapsed" value={`${receipt.elapsed_ms?.toFixed(0)}ms`} />
            <Row label="Results" value={String(receipt.result_count)} />
            {receipt.output_summary && (
              <div className="mt-1 text-slate-300 line-clamp-3">{receipt.output_summary}</div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-300 text-right truncate max-w-[150px]">{String(value) || '—'}</span>
    </div>
  )
}

export function ExecutionGraph() {
  const { graph, selectedNodeId, selectNode } = useInvestigationStore()
  const [nodes, setNodes, onNodesChange] = useNodesState([] as Node[])
  const [edges, setEdges, onEdgesChange] = useEdgesState([] as Edge[])

  useEffect(() => {
    if (!graph) return
    setNodes(convertToFlowNodes(graph.nodes))
    setEdges(convertToFlowEdges(graph.edges))
  }, [graph, setNodes, setEdges])

  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      selectNode(node.id === selectedNodeId ? null : node.id)
    },
    [selectNode, selectedNodeId]
  )

  if (!graph) {
    return (
      <div className="flex items-center justify-center h-full text-slate-600 text-sm">
        No graph data yet. Waiting for events...
      </div>
    )
  }

  return (
    <div className="flex h-full">
      {/* Graph canvas */}
      <div className="flex-1 relative">
        {graph.has_gaps && (
          <div className="absolute top-3 left-3 z-10 flex items-center gap-2 bg-orange-900/80 border border-orange-700 rounded px-3 py-1.5 text-xs text-orange-300">
            ⚠️ {graph.event_gaps.length} event gaps detected
          </div>
        )}
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={onNodeClick}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          colorMode="dark"
          defaultEdgeOptions={{ type: 'smoothstep' }}
          minZoom={0.3}
          maxZoom={2}
        >
          <Background variant={BackgroundVariant.Dots} color="#1e293b" gap={20} size={1} />
          <Controls
            style={{ background: '#1e293b', border: '1px solid #334155' }}
          />
          <MiniMap
            style={{ background: '#0f172a', border: '1px solid #1e293b' }}
            nodeColor={(node) => {
              const graphNode = graph.nodes.find((n) => n.node_id === node.id)
              return graphNode ? NODE_COLORS[graphNode.status]?.border ?? '#475569' : '#475569'
            }}
          />
        </ReactFlow>

        {/* Graph stats */}
        <div className="absolute bottom-3 left-3 flex gap-2 text-xs">
          <span className="bg-slate-900/90 border border-slate-800 px-2 py-1 rounded text-slate-400">
            {graph.nodes.length} nodes
          </span>
          <span className="bg-slate-900/90 border border-slate-800 px-2 py-1 rounded text-slate-400">
            {graph.edges.length} edges
          </span>
          {graph.is_complete && (
            <span className="bg-green-900/50 border border-green-800 px-2 py-1 rounded text-green-400">
              Complete
            </span>
          )}
        </div>
      </div>

      {/* Node detail panel */}
      {selectedNodeId && <NodeDetailPanel nodeId={selectedNodeId} />}
    </div>
  )
}
