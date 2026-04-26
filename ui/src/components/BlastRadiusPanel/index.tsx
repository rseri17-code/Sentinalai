// Blast Radius Panel — visualizes failure cascade scope before fix approval
// Shows affected services, risk tier, impact score, and safe-to-apply determination.
// Displayed in the ControlPanel when a fix is proposed, letting the approver
// understand the blast radius BEFORE clicking "Approve Fix".

import React, { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle, XCircle, Zap, Shield } from 'lucide-react'
import clsx from 'clsx'

// ---------------------------------------------------------------------------
// Types (mirrors supervisor/blast_radius.py)
// ---------------------------------------------------------------------------

type RiskTier = 'LOW' | 'MEDIUM' | 'HIGH' | 'CRITICAL'

interface AffectedService {
  service: string
  risk_tier: RiskTier
  impact_score: number
  hop_distance: number
  downstream_count: number
  has_p1_dependency: boolean
  precautions: string[]
}

interface BlastRadiusReport {
  target_service: string
  fix_type: string
  risk_tier: RiskTier
  safe_to_auto_apply: boolean
  affected_service_count: number
  p1_dependency_count: number
  affected_services: AffectedService[]
  precautions: string[]
  reasoning: string
  generated_at: string
}

interface Props {
  investigationId: string
  onSafeToApply?: (safe: boolean) => void
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

const TIER_CONFIG: Record<RiskTier, { color: string; bg: string; label: string }> = {
  LOW:      { color: 'text-green-400',  bg: 'bg-green-900/40',  label: 'Low Risk' },
  MEDIUM:   { color: 'text-yellow-400', bg: 'bg-yellow-900/40', label: 'Medium Risk' },
  HIGH:     { color: 'text-orange-400', bg: 'bg-orange-900/40', label: 'High Risk' },
  CRITICAL: { color: 'text-red-400',    bg: 'bg-red-900/40',    label: 'Critical Risk' },
}

function TierBadge({ tier }: { tier: RiskTier }) {
  const cfg = TIER_CONFIG[tier] ?? TIER_CONFIG.MEDIUM
  return (
    <span className={clsx('px-2 py-0.5 rounded text-xs font-bold uppercase', cfg.color, cfg.bg)}>
      {cfg.label}
    </span>
  )
}

function ImpactBar({ score, maxScore = 100 }: { score: number; maxScore?: number }) {
  const pct = Math.min(100, (score / maxScore) * 100)
  const color = score >= 75 ? 'bg-red-500' : score >= 40 ? 'bg-yellow-500' : 'bg-green-500'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all', color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-slate-400 w-10 text-right">{score.toFixed(0)}</span>
    </div>
  )
}

function ServiceRow({ svc }: { svc: AffectedService }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="border border-slate-700 rounded-lg overflow-hidden">
      <button
        className="w-full flex items-center gap-3 px-3 py-2 hover:bg-slate-800 transition-colors text-left"
        onClick={() => setExpanded(e => !e)}
      >
        <TierBadge tier={svc.risk_tier} />
        <span className="flex-1 font-mono text-sm text-slate-200">{svc.service}</span>
        {svc.has_p1_dependency && (
          <span className="text-xs text-red-400 font-medium">P1 Dep</span>
        )}
        <span className="text-xs text-slate-500">hop {svc.hop_distance}</span>
        <ImpactBar score={svc.impact_score} />
      </button>
      {expanded && svc.precautions.length > 0 && (
        <div className="px-3 pb-3 bg-slate-900/50">
          <p className="text-xs text-slate-500 mb-1 mt-2">Precautions:</p>
          <ul className="space-y-1">
            {svc.precautions.map((p, i) => (
              <li key={i} className="flex gap-2 text-xs text-slate-300">
                <span className="text-yellow-500 mt-0.5">•</span>
                {p}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

export default function BlastRadiusPanel({ investigationId, onSafeToApply }: Props) {
  const [report, setReport] = useState<BlastRadiusReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const token = localStorage.getItem('agui_token')
        const res = await fetch(`/api/v1/investigations/${investigationId}/blast-radius`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        if (!res.ok) throw new Error(`${res.status}`)
        const data: BlastRadiusReport = await res.json()
        if (!cancelled) {
          setReport(data)
          onSafeToApply?.(data.safe_to_auto_apply)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => { cancelled = true }
  }, [investigationId, onSafeToApply])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-32 text-slate-500 text-sm">
        Computing blast radius...
      </div>
    )
  }

  if (error || !report) {
    return (
      <div className="flex items-center gap-2 text-slate-500 text-sm p-4">
        <AlertTriangle className="h-4 w-4" />
        Blast radius unavailable — proceed with caution
      </div>
    )
  }

  const tier = TIER_CONFIG[report.risk_tier] ?? TIER_CONFIG.MEDIUM

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className={clsx('rounded-lg p-4 border', tier.bg,
        report.risk_tier === 'CRITICAL' ? 'border-red-700' :
        report.risk_tier === 'HIGH'     ? 'border-orange-700' :
        report.risk_tier === 'MEDIUM'   ? 'border-yellow-700' : 'border-green-700'
      )}>
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Zap className={clsx('h-5 w-5', tier.color)} />
            <span className="font-semibold text-slate-100">Blast Radius Assessment</span>
          </div>
          <TierBadge tier={report.risk_tier} />
        </div>

        <div className="grid grid-cols-3 gap-4 text-center">
          <div>
            <p className="text-2xl font-bold text-slate-100">{report.affected_service_count}</p>
            <p className="text-xs text-slate-400">Services at risk</p>
          </div>
          <div>
            <p className="text-2xl font-bold text-red-400">{report.p1_dependency_count}</p>
            <p className="text-xs text-slate-400">P1 dependencies</p>
          </div>
          <div>
            {report.safe_to_auto_apply ? (
              <>
                <CheckCircle className="h-6 w-6 text-green-400 mx-auto" />
                <p className="text-xs text-green-400 mt-1">Safe to apply</p>
              </>
            ) : (
              <>
                <Shield className="h-6 w-6 text-orange-400 mx-auto" />
                <p className="text-xs text-orange-400 mt-1">Approval required</p>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Reasoning */}
      {report.reasoning && (
        <p className="text-xs text-slate-400 leading-relaxed">{report.reasoning}</p>
      )}

      {/* Global precautions */}
      {report.precautions.length > 0 && (
        <div className="bg-slate-800 rounded-lg p-3">
          <p className="text-xs font-medium text-slate-300 mb-2">Required precautions</p>
          <ul className="space-y-1">
            {report.precautions.map((p, i) => (
              <li key={i} className="flex gap-2 text-xs text-slate-300">
                <AlertTriangle className="h-3 w-3 text-yellow-500 mt-0.5 flex-shrink-0" />
                {p}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Affected services */}
      {report.affected_services.length > 0 && (
        <div>
          <p className="text-xs font-medium text-slate-400 mb-2 uppercase tracking-wide">
            Affected Services
          </p>
          <div className="space-y-2">
            {report.affected_services.map(svc => (
              <ServiceRow key={svc.service} svc={svc} />
            ))}
          </div>
        </div>
      )}

      {/* Safe to apply banner */}
      {report.safe_to_auto_apply ? (
        <div className="flex items-center gap-2 bg-green-900/30 border border-green-700 rounded-lg p-3">
          <CheckCircle className="h-4 w-4 text-green-400 flex-shrink-0" />
          <p className="text-xs text-green-300">
            Low blast radius — no P1 dependencies affected. SentinalAI considers this safe to auto-apply.
          </p>
        </div>
      ) : (
        <div className="flex items-center gap-2 bg-orange-900/20 border border-orange-700 rounded-lg p-3">
          <XCircle className="h-4 w-4 text-orange-400 flex-shrink-0" />
          <p className="text-xs text-orange-300">
            Manual approval required — {report.p1_dependency_count} P1{' '}
            {report.p1_dependency_count === 1 ? 'dependency' : 'dependencies'} in blast radius.
          </p>
        </div>
      )}
    </div>
  )
}
