import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, AlertTriangle, CheckCircle, ShieldCheck, ShieldOff, ArrowRight, RefreshCw } from 'lucide-react'
import { operationalHealthApi, type OperationalHealthResponse, type ServiceHealth } from '@/api/client'
import clsx from 'clsx'

const BAND_STYLE: Record<string, { dot: string; text: string; label: string }> = {
  healthy: { dot: 'bg-green-500', text: 'text-green-400', label: 'Healthy' },
  watch: { dot: 'bg-yellow-500', text: 'text-yellow-400', label: 'Watch' },
  at_risk: { dot: 'bg-red-500', text: 'text-red-400', label: 'At risk' },
}

function ServiceRow({ svc, investigationId }: { svc: ServiceHealth; investigationId?: string }) {
  const band = BAND_STYLE[svc.health_band] ?? BAND_STYLE.watch
  return (
    <div className="flex items-center gap-4 px-4 py-3 border-b border-slate-800 hover:bg-slate-900/50">
      <span className={clsx('w-2.5 h-2.5 rounded-full shrink-0', band.dot)} />
      <div className="w-48 shrink-0">
        <div className="text-sm font-medium text-slate-100 truncate">{svc.service}</div>
        <div className={clsx('text-xs', band.text)}>{band.label} · {(svc.health_score * 100).toFixed(0)}%</div>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-sm text-slate-300 truncate">{svc.why || '(unresolved)'}</div>
        <div className="text-xs text-slate-500 mt-0.5">
          {svc.incidents} investigation{svc.incidents === 1 ? '' : 's'} · {svc.next_action}
        </div>
      </div>
      <div className="w-24 shrink-0 text-right">
        <div className="text-xs text-slate-400">conf {svc.confidence}</div>
        <div className="text-xs flex items-center justify-end gap-1 mt-0.5">
          {svc.verifiable
            ? <span className="text-green-500 flex items-center gap-1"><ShieldCheck size={12} /> verifiable</span>
            : <span className="text-slate-500 flex items-center gap-1"><ShieldOff size={12} /> unverified</span>}
        </div>
      </div>
      <div className="w-40 shrink-0 text-right text-xs text-slate-500">
        ev {svc.evidence.used} used
        {svc.evidence.unavailable > 0 && <span className="text-amber-500"> · {svc.evidence.unavailable} unavailable</span>}
      </div>
      <div className="w-32 shrink-0 text-right">
        {investigationId ? (
          <Link
            to={`/investigations/${investigationId}`}
            className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
          >
            Open investigation <ArrowRight size={12} />
          </Link>
        ) : (
          <span className="text-xs text-slate-600">no investigation</span>
        )}
      </div>
    </div>
  )
}

export function OperationalHealth() {
  const [data, setData] = useState<OperationalHealthResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = React.useCallback(() => {
    setLoading(true)
    setError(null)
    operationalHealthApi
      .get()
      .then((d) => setData(d))
      .catch((e) => setError(e?.message || 'Failed to load operational health'))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load() }, [load])

  const services = data
    ? data.attention_order.map((s) => data.services[s]).filter(Boolean)
    : []

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-2 border-b border-slate-800 bg-slate-900/60 shrink-0 flex items-center gap-2">
        <Activity size={15} className="text-blue-400" />
        <span className="text-sm font-medium text-slate-300">Operational Health</span>
        <span className="text-[10px] text-slate-500 ml-1">— per-service, worst first, from completed investigations</span>
        <button onClick={load} className="ml-auto text-slate-500 hover:text-slate-300" title="Refresh">
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
        </button>
      </div>

      {/* Estate summary */}
      {data && (
        <div className="px-4 py-3 border-b border-slate-800 flex items-center gap-6 text-sm">
          <div className="text-slate-400">
            Estate score{' '}
            <span className="text-slate-100 font-semibold">
              {data.estate_health_score != null ? (data.estate_health_score * 100).toFixed(0) + '%' : '—'}
            </span>
          </div>
          <div className="flex items-center gap-3 text-xs">
            <span className="flex items-center gap-1 text-green-400"><CheckCircle size={12} /> {data.band_counts.healthy ?? 0}</span>
            <span className="flex items-center gap-1 text-yellow-400"><Activity size={12} /> {data.band_counts.watch ?? 0}</span>
            <span className="flex items-center gap-1 text-red-400"><AlertTriangle size={12} /> {data.band_counts.at_risk ?? 0}</span>
          </div>
          <div className="text-xs text-slate-500">{data.investigations} investigation(s) · {data.services_evaluated} service(s)</div>
        </div>
      )}

      {/* Honest signal-coverage disclosure */}
      {data && data.signal_coverage.with_validation_signals < data.signal_coverage.investigations && (
        <div className="px-4 py-2 bg-amber-950/30 border-b border-amber-900/40 text-xs text-amber-300/90 flex items-start gap-2">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" />
          <span>{data.signal_coverage.note}</span>
        </div>
      )}

      <div className="flex-1 overflow-auto">
        {loading && <div className="p-8 text-center text-slate-500 text-sm">Loading operational health…</div>}
        {error && !loading && (
          <div className="p-8 text-center text-red-400 text-sm">
            {error}
            <div><button onClick={load} className="mt-2 text-blue-400 hover:text-blue-300 text-xs">Retry</button></div>
          </div>
        )}
        {!loading && !error && services.length === 0 && (
          <div className="p-8 text-center text-slate-500 text-sm">
            No completed investigations yet. Health appears here once investigations complete.
          </div>
        )}
        {!loading && !error && services.map((svc) => (
          <ServiceRow key={svc.service} svc={svc} investigationId={data?.drilldown[svc.service]} />
        ))}
      </div>
    </div>
  )
}

export default OperationalHealth
