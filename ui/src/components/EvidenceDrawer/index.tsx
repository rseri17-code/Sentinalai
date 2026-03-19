// Panel 3: Evidence Drawer
// Shows all receipts (tool call evidence) with full details

import React, { useState } from 'react'
import { useInvestigationStore } from '@/store/investigationStore'
import type { UIReceipt } from '@/types'
import clsx from 'clsx'
import { CheckCircle, XCircle, Clock, AlertCircle, ChevronDown, ChevronRight, Shield } from 'lucide-react'

const STATUS_ICON: Record<string, React.ReactNode> = {
  success: <CheckCircle size={13} className="text-green-400" />,
  error: <XCircle size={13} className="text-red-400" />,
  timeout: <Clock size={13} className="text-orange-400" />,
  pending: <AlertCircle size={13} className="text-slate-400" />,
}

function ReceiptCard({ receipt }: { receipt: UIReceipt }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className={clsx(
      'border rounded text-xs',
      receipt.status === 'success' ? 'border-slate-700 bg-slate-900' : 'border-red-900/50 bg-red-950/20'
    )}>
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2.5 hover:bg-slate-800/50 rounded-t text-left"
      >
        {STATUS_ICON[receipt.status] || STATUS_ICON.pending}
        <span className="font-medium text-slate-200 flex-1 min-w-0 truncate">{receipt.worker}</span>
        <span className="text-slate-500">{receipt.action}</span>
        <span className={clsx('ml-2 font-mono', receipt.elapsed_ms > 1000 ? 'text-orange-400' : 'text-slate-500')}>
          {receipt.elapsed_ms?.toFixed(0)}ms
        </span>
        {expanded ? <ChevronDown size={12} className="text-slate-500 shrink-0" /> : <ChevronRight size={12} className="text-slate-500 shrink-0" />}
      </button>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-slate-800 px-3 py-2 space-y-2">
          <DetailRow label="Tool" value={receipt.tool} />
          <DetailRow label="Receipt ID" value={receipt.receipt_id} mono />
          <DetailRow label="Trace ID" value={receipt.trace_id} mono />
          <DetailRow label="Correlation" value={receipt.correlation_id} mono />
          <DetailRow label="Started" value={new Date(receipt.wall_clock_start).toLocaleTimeString()} />
          <DetailRow label="Ended" value={new Date(receipt.wall_clock_end).toLocaleTimeString()} />
          <DetailRow label="Results" value={String(receipt.result_count)} />
          <DetailRow label="Sequence" value={String(receipt.sequence_num)} />

          {receipt.error && (
            <div>
              <div className="text-slate-500 mb-0.5">Error</div>
              <div className="text-red-400 font-mono break-words">{receipt.error}</div>
            </div>
          )}

          {receipt.output_summary && (
            <div>
              <div className="text-slate-500 mb-0.5">Output Summary</div>
              <div className="text-slate-300 line-clamp-4 leading-relaxed">{receipt.output_summary}</div>
            </div>
          )}

          {/* Params (redacted) */}
          {Object.keys(receipt.params_redacted || {}).length > 0 && (
            <div>
              <div className="text-slate-500 mb-0.5 flex items-center gap-1">
                <Shield size={10} />
                Params (redacted)
              </div>
              <pre className="text-slate-400 bg-slate-950 rounded p-2 overflow-x-auto text-[10px] max-h-24">
                {JSON.stringify(receipt.params_redacted, null, 2)}
              </pre>
            </div>
          )}

          {/* Integrity */}
          <div className="flex items-center gap-1 text-slate-600 text-[10px]">
            <Shield size={9} />
            Hash: <span className="font-mono">{receipt.payload_hash?.slice(0, 16)}...</span>
            {receipt.storage_uri && (
              <span className="ml-2 truncate">{receipt.storage_uri}</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function DetailRow({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between gap-2 text-[11px]">
      <span className="text-slate-500 shrink-0">{label}</span>
      <span className={clsx('text-right truncate max-w-[220px]', mono ? 'font-mono text-slate-400' : 'text-slate-300')}>
        {value || '—'}
      </span>
    </div>
  )
}

export function EvidenceDrawer() {
  const { receipts, investigation } = useInvestigationStore()
  const [filter, setFilter] = useState<'all' | 'success' | 'error'>('all')
  const [search, setSearch] = useState('')

  const filtered = receipts
    .filter((r) => filter === 'all' || r.status === filter)
    .filter((r) =>
      !search ||
      r.worker.toLowerCase().includes(search.toLowerCase()) ||
      r.action.toLowerCase().includes(search.toLowerCase())
    )
    .sort((a, b) => a.sequence_num - b.sequence_num)

  const successCount = receipts.filter((r) => r.status === 'success').length
  const failedCount = receipts.filter((r) => r.status === 'error' || r.status === 'timeout').length

  return (
    <div className="flex flex-col h-full p-4">
      <div className="panel-header rounded-t-lg border border-slate-800 bg-slate-900 mb-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-slate-200">Evidence Receipts</span>
          <span className="badge bg-slate-800 text-slate-400">{receipts.length}</span>
          <span className="badge bg-green-900/50 text-green-400">{successCount} ok</span>
          {failedCount > 0 && (
            <span className="badge bg-red-900/50 text-red-400">{failedCount} failed</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            placeholder="Filter..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs text-slate-300 focus:outline-none focus:border-blue-600 w-32"
          />
          <div className="flex gap-1">
            {(['all', 'success', 'error'] as const).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={clsx(
                  'btn text-xs py-1',
                  filter === f ? 'bg-slate-700 text-slate-200' : 'bg-transparent text-slate-500 hover:text-slate-300'
                )}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Coverage indicator */}
      {investigation && (
        <div className="border-x border-slate-800 bg-slate-900 px-4 py-2">
          <div className="flex items-center gap-2 text-xs">
            <span className="text-slate-500">Evidence coverage</span>
            <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
              <div
                className="h-full bg-blue-500 rounded-full"
                style={{
                  width: `${investigation.tool_calls_total > 0
                    ? (receipts.length / investigation.tool_calls_total) * 100
                    : 0}%`,
                }}
              />
            </div>
            <span className="text-slate-400">{receipts.length}/{investigation.tool_calls_total}</span>
          </div>
        </div>
      )}

      <div className="flex-1 scroll-panel border border-t-0 border-slate-800 rounded-b-lg bg-slate-900 p-3 space-y-2">
        {filtered.length === 0 ? (
          <div className="text-center text-slate-600 py-8 text-sm">
            {receipts.length === 0 ? 'No receipts yet.' : 'No receipts match filter.'}
          </div>
        ) : (
          filtered.map((receipt) => (
            <ReceiptCard key={receipt.receipt_id} receipt={receipt} />
          ))
        )}
      </div>

      {/* No black box guarantee */}
      <div className="mt-2 text-[10px] text-slate-600 flex items-center gap-1.5 px-1">
        <Shield size={10} />
        Every tool call has a receipt. No black box. All evidence is traceable.
      </div>
    </div>
  )
}
