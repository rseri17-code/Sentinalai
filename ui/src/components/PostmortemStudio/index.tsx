// Postmortem Studio — blameless postmortem generation, editing, and approval
// Auto-filled from RCA data. Engineers edit narrative sections before publishing.
// Approval is required before publishing to Confluence. Human owns the decision.

import React, { useEffect, useState, useCallback } from 'react'
import {
  FileText, CheckCircle, Clock, Edit3, Send, ChevronDown, ChevronRight, User
} from 'lucide-react'
import { formatDistanceToNow, format } from 'date-fns'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types (mirrors supervisor/postmortem_generator.py)
// ---------------------------------------------------------------------------

type PostmortemStatus = 'draft' | 'approved'

interface ActionItem {
  title: string
  description: string
  priority: 'P1' | 'P2' | 'P3'
  category: 'prevention' | 'detection' | 'response' | 'documentation'
  owner: string
  due_days: number
  estimated_effort: 'hours' | 'days' | 'weeks'
}

interface PostmortemReport {
  report_id: string
  incident_id: string
  generated_at: string
  status: PostmortemStatus
  reviewed_by: string | null
  severity: string
  affected_service: string
  duration_minutes: number
  executive_summary: string
  impact_statement: string
  timeline: { time: string; event: string; source: string }[]
  contributing_factors: string[]
  what_went_well: string[]
  what_needs_improvement: string[]
  five_whys: string[]
  action_items: ActionItem[]
  prevention_recommendations: string[]
  similar_past_incidents: { incident_id: string; root_cause: string; similarity: number }[]
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

const PRIORITY_CONFIG = {
  P1: { color: 'text-red-400',    bg: 'bg-red-900/30',    border: 'border-red-700' },
  P2: { color: 'text-yellow-400', bg: 'bg-yellow-900/30', border: 'border-yellow-700' },
  P3: { color: 'text-blue-400',   bg: 'bg-blue-900/20',   border: 'border-blue-700' },
}

const CATEGORY_LABELS = {
  prevention:    'Prevention',
  detection:     'Detection',
  response:      'Response',
  documentation: 'Documentation',
}

function Section({
  title, icon, children, defaultOpen = true
}: {
  title: string
  icon?: React.ReactNode
  children: React.ReactNode
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border border-slate-700 rounded-xl overflow-hidden">
      <button
        className="w-full flex items-center gap-2 px-4 py-3 bg-slate-800/60 hover:bg-slate-800 transition-colors text-left"
        onClick={() => setOpen(o => !o)}
      >
        {icon && <span className="text-slate-400">{icon}</span>}
        <span className="flex-1 text-sm font-semibold text-slate-200">{title}</span>
        {open ? <ChevronDown className="h-4 w-4 text-slate-500" /> : <ChevronRight className="h-4 w-4 text-slate-500" />}
      </button>
      {open && <div className="px-4 py-3">{children}</div>}
    </div>
  )
}

function EditableText({
  value, onChange, rows = 3
}: {
  value: string; onChange: (v: string) => void; rows?: number
}) {
  return (
    <textarea
      value={value}
      onChange={e => onChange(e.target.value)}
      rows={rows}
      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-2 text-sm
                 text-slate-200 placeholder-slate-500 resize-none
                 focus:outline-none focus:border-blue-500 transition-colors"
    />
  )
}

function ActionItemCard({ item, onUpdate }: {
  item: ActionItem; onUpdate: (item: ActionItem) => void
}) {
  const cfg = PRIORITY_CONFIG[item.priority]
  return (
    <div className={clsx('rounded-lg border p-3 space-y-2', cfg.bg, cfg.border)}>
      <div className="flex items-center gap-2">
        <span className={clsx('text-xs font-bold uppercase', cfg.color)}>{item.priority}</span>
        <span className="text-xs text-slate-500 bg-slate-700 rounded px-1.5 py-0.5">
          {CATEGORY_LABELS[item.category]}
        </span>
        <span className="ml-auto text-xs text-slate-500">{item.estimated_effort}</span>
      </div>
      <input
        type="text"
        value={item.title}
        onChange={e => onUpdate({ ...item, title: e.target.value })}
        className="w-full bg-transparent border-b border-slate-700 text-sm text-slate-200
                   focus:outline-none focus:border-blue-500 pb-0.5"
      />
      <div className="flex items-center gap-4 text-xs text-slate-500">
        <span>Owner: {item.owner || '—'}</span>
        <span>Due in {item.due_days} days</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface Props {
  investigationId?: string
  incidentId?: string
}

export default function PostmortemStudio({ investigationId, incidentId }: Props) {
  const [report, setReport] = useState<PostmortemReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [approving, setApproving] = useState(false)
  const [publishing, setPublishing] = useState(false)
  const [error, setError] = useState('')
  const [dirty, setDirty] = useState(false)

  const token = localStorage.getItem('agui_token')
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }

  const generate = useCallback(async () => {
    if (!incidentId && !investigationId) return
    setGenerating(true)
    setError('')
    try {
      const res = await fetch('/api/v1/postmortem', {
        method: 'POST',
        headers,
        body: JSON.stringify({ incident_id: incidentId, investigation_id: investigationId }),
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      const data: PostmortemReport = await res.json()
      setReport(data)
      setDirty(false)
    } catch (e) {
      setError(String(e))
    } finally {
      setGenerating(false)
    }
  }, [incidentId, investigationId])

  useEffect(() => {
    if (incidentId || investigationId) generate()
  }, [generate])

  const update = <K extends keyof PostmortemReport>(key: K, value: PostmortemReport[K]) => {
    setReport(prev => prev ? { ...prev, [key]: value } : prev)
    setDirty(true)
  }

  const approve = async () => {
    if (!report) return
    setApproving(true)
    try {
      await fetch(`/api/v1/postmortem/${report.report_id}/approve`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ reviewer: 'operator' }),
      })
      setReport(prev => prev ? { ...prev, status: 'approved', reviewed_by: 'operator' } : prev)
      setDirty(false)
    } finally {
      setApproving(false)
    }
  }

  const publish = async () => {
    if (!report) return
    setPublishing(true)
    try {
      await fetch(`/api/v1/postmortem/${report.report_id}/publish`, {
        method: 'POST',
        headers,
      })
    } finally {
      setPublishing(false)
    }
  }

  if (loading || generating) {
    return (
      <div className="flex items-center justify-center h-40 text-slate-500 text-sm">
        {generating ? 'Generating postmortem from RCA data...' : 'Loading...'}
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-6 text-center space-y-3">
        <p className="text-red-400 text-sm">{error}</p>
        <button
          onClick={generate}
          className="px-4 py-2 bg-blue-700 hover:bg-blue-600 text-white text-sm rounded-lg"
        >
          Retry
        </button>
      </div>
    )
  }

  if (!report) {
    return (
      <div className="flex flex-col items-center justify-center h-40 gap-3">
        <FileText className="h-8 w-8 text-slate-600" />
        <p className="text-sm text-slate-500">No postmortem generated yet</p>
        <button
          onClick={generate}
          className="px-4 py-2 bg-blue-700 hover:bg-blue-600 text-white text-sm rounded-lg"
        >
          Generate Postmortem
        </button>
      </div>
    )
  }

  const isApproved = report.status === 'approved'

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
        <div className="flex items-center gap-3">
          <FileText className="h-4 w-4 text-blue-400" />
          <div>
            <p className="text-sm font-semibold text-slate-100">
              Postmortem — {report.incident_id}
            </p>
            <p className="text-xs text-slate-500">
              {report.affected_service} · {report.duration_minutes}min duration
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {/* Status badge */}
          {isApproved ? (
            <span className="flex items-center gap-1 text-xs text-green-400 bg-green-900/30 border border-green-700 rounded-full px-2 py-0.5">
              <CheckCircle className="h-3 w-3" />
              Approved by {report.reviewed_by}
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs text-yellow-400 bg-yellow-900/20 border border-yellow-700 rounded-full px-2 py-0.5">
              <Edit3 className="h-3 w-3" />
              Draft
            </span>
          )}

          {dirty && <span className="text-xs text-slate-500">Unsaved changes</span>}

          {!isApproved && (
            <button
              onClick={approve}
              disabled={approving}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600
                         disabled:opacity-50 text-white text-xs font-medium rounded-lg transition-colors"
            >
              <CheckCircle className="h-3 w-3" />
              {approving ? 'Approving...' : 'Approve'}
            </button>
          )}

          <button
            onClick={publish}
            disabled={!isApproved || publishing}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-700 hover:bg-blue-600
                       disabled:opacity-40 disabled:cursor-not-allowed
                       text-white text-xs font-medium rounded-lg transition-colors"
            title={!isApproved ? 'Approve before publishing' : 'Publish to Confluence'}
          >
            <Send className="h-3 w-3" />
            {publishing ? 'Publishing...' : 'Publish'}
          </button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">

        <Section title="Executive Summary" defaultOpen={true}>
          <EditableText
            value={report.executive_summary}
            onChange={v => update('executive_summary', v)}
            rows={4}
          />
        </Section>

        <Section title="Impact Statement">
          <EditableText
            value={report.impact_statement}
            onChange={v => update('impact_statement', v)}
            rows={2}
          />
        </Section>

        <Section title="Timeline" defaultOpen={true}>
          <div className="space-y-2">
            {report.timeline.map((entry, i) => (
              <div key={i} className="flex gap-3 text-xs">
                <span className="text-slate-500 font-mono w-20 flex-shrink-0">
                  {entry.time?.slice(11, 19) ?? '—'}
                </span>
                <span className="text-slate-300 flex-1">{entry.event}</span>
                <span className="text-slate-600 flex-shrink-0">{entry.source}</span>
              </div>
            ))}
          </div>
        </Section>

        <Section title="5 Whys">
          <div className="space-y-3">
            {report.five_whys.map((why, i) => (
              <div key={i} className="flex gap-3">
                <span className="text-blue-500 font-bold text-sm flex-shrink-0 pt-0.5">
                  {i + 1}.
                </span>
                <textarea
                  value={why}
                  onChange={e => {
                    const updated = [...report.five_whys]
                    updated[i] = e.target.value
                    update('five_whys', updated)
                  }}
                  rows={2}
                  className="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-3 py-2
                             text-sm text-slate-200 resize-none focus:outline-none focus:border-blue-500"
                />
              </div>
            ))}
          </div>
        </Section>

        <Section title="Contributing Factors">
          <ul className="space-y-1.5">
            {report.contributing_factors.map((f, i) => (
              <li key={i} className="flex gap-2 text-xs text-slate-300">
                <span className="text-yellow-500 mt-0.5">•</span>
                {f}
              </li>
            ))}
          </ul>
        </Section>

        <div className="grid grid-cols-2 gap-4">
          <Section title="What Went Well">
            <ul className="space-y-1.5">
              {report.what_went_well.map((f, i) => (
                <li key={i} className="flex gap-2 text-xs text-slate-300">
                  <span className="text-green-500 mt-0.5">+</span>{f}
                </li>
              ))}
            </ul>
          </Section>
          <Section title="What Needs Improvement">
            <ul className="space-y-1.5">
              {report.what_needs_improvement.map((f, i) => (
                <li key={i} className="flex gap-2 text-xs text-slate-300">
                  <span className="text-orange-500 mt-0.5">→</span>{f}
                </li>
              ))}
            </ul>
          </Section>
        </div>

        <Section title={`Action Items (${report.action_items.length})`} defaultOpen={true}>
          <div className="space-y-3">
            {report.action_items.map((item, i) => (
              <ActionItemCard
                key={i}
                item={item}
                onUpdate={updated => {
                  const items = [...report.action_items]
                  items[i] = updated
                  update('action_items', items)
                }}
              />
            ))}
          </div>
        </Section>

        {report.prevention_recommendations.length > 0 && (
          <Section title="Prevention Recommendations">
            <ul className="space-y-1.5">
              {report.prevention_recommendations.map((r, i) => (
                <li key={i} className="flex gap-2 text-xs text-slate-300">
                  <span className="text-blue-500 mt-0.5">→</span>{r}
                </li>
              ))}
            </ul>
          </Section>
        )}

        <div className="text-xs text-slate-600 text-center pb-2">
          Generated by SentinalAI Postmortem Studio · Blameless by design ·{' '}
          {formatDistanceToNow(new Date(report.generated_at), { addSuffix: true })}
        </div>
      </div>
    </div>
  )
}
