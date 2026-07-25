import React from 'react'
import { useInvestigationStore } from '@/store/investigationStore'

// Persistent Investigation Summary Header (Phase 2, Iteration 1).
// Answers the five operator questions without opening a panel, using ONLY
// fields already present on the investigation model. It derives nothing,
// summarizes nothing with AI, and adds no backend call. Flat, semantic,
// keyboard/screen-reader friendly, responsive; no animation/gradient/card/modal.

function Field({ label, value, title }: { label: string; value: React.ReactNode; title?: string }) {
  return (
    <div className="min-w-0" role="group" aria-label={label}>
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-sm text-slate-200 truncate" title={title}>{value}</div>
    </div>
  )
}

export function InvestigationSummary() {
  const { investigation, setActivePanel } = useInvestigationStore()
  if (!investigation) return null

  const inv = investigation
  const confPct = inv.confidence > 1
    ? Math.round(inv.confidence)
    : Math.round((inv.confidence || 0) * 100)

  // "What proves it" — the winning hypothesis's own evidence, else receipts.
  const winner = inv.hypotheses?.find((h) => h.name === inv.winner_hypothesis)
  const evidenceRefs = (winner?.evidence_refs && winner.evidence_refs.length
    ? winner.evidence_refs
    : inv.receipt_ids) || []
  const evidenceCount = evidenceRefs.length

  const nextAction = inv.awaiting_approval_for || '—'
  const rootCause = inv.root_cause || '—'

  return (
    <header
      role="region"
      aria-label="Investigation summary"
      className="border-b border-slate-800 bg-slate-900/70 px-4 py-2 shrink-0"
    >
      <h2 className="sr-only">Investigation summary</h2>
      <div className="flex flex-wrap items-start gap-x-6 gap-y-1">
        {/* WHAT */}
        <Field
          label="Status"
          value={<span className="capitalize">{inv.status}</span>}
        />
        <Field label="Service (owner)" value={inv.affected_service || '—'}
               title={inv.affected_service} />
        {/* WHY */}
        <div className="min-w-0 flex-1 basis-64" role="group" aria-label="Root cause">
          <div className="text-[10px] uppercase tracking-wider text-slate-500">Root cause</div>
          <div className="text-sm text-slate-100 truncate" title={rootCause}>{rootCause}</div>
        </div>
        {/* CONFIDENCE */}
        <Field
          label="Confidence"
          value={<span aria-label={`Confidence ${confPct} percent`}>{confPct}%</span>}
        />
        {/* WHAT PROVES IT */}
        <Field
          label="Evidence"
          value={
            <button
              type="button"
              onClick={() => setActivePanel('evidence')}
              className="text-blue-400 hover:text-blue-300 underline-offset-2 hover:underline focus:underline focus:outline-none"
              aria-label={`${evidenceCount} supporting evidence items; open evidence panel`}
            >
              {evidenceCount} item{evidenceCount === 1 ? '' : 's'}
            </button>
          }
        />
        {/* WHAT NEXT */}
        <Field label="Next action" value={nextAction} title={nextAction} />
        {/* VERIFIABLE */}
        <Field
          label="Verification"
          value={inv.replay_available
            ? <span className="text-green-400">Replayable</span>
            : <span className="text-slate-400">Not replayable</span>}
        />
      </div>
    </header>
  )
}

export default InvestigationSummary
