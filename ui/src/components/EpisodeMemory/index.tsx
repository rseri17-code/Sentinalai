import { useEffect, useState } from 'react'
import { Clock } from 'lucide-react'

interface Episode {
  id: string
  service: string
  incident_type: string
  failure_signature: string
  resolution: string
  ttresolve_seconds: number
  outcome: string
  confidence: number
  occurred_at: string
}

interface EpisodeMemoryProps {
  service?: string
  incidentType?: string
  compact?: boolean
}

function outcomeColor(outcome: string): string {
  if (outcome === 'auto-remediated' || outcome === 'auto-fixed') return 'text-emerald-400 bg-emerald-950/40 border-emerald-800/50'
  if (outcome === 'escalated') return 'text-red-400 bg-red-950/40 border-red-800/50'
  return 'text-blue-400 bg-blue-950/40 border-blue-800/50'
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}m ${s}s`
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  const days = Math.floor(diff / 86400000)
  if (days > 0) return `${days} day${days > 1 ? 's' : ''} ago`
  const hours = Math.floor(diff / 3600000)
  if (hours > 0) return `${hours}h ago`
  return 'recently'
}

const DEMO_EPISODES: Episode[] = [
  { id: '1', service: 'payment-service', incident_type: 'timeout', failure_signature: 'postgres connection pool exhausted', resolution: 'increase pool size from 10 to 50', ttresolve_seconds: 503, outcome: 'auto-fixed', confidence: 0.87, occurred_at: new Date(Date.now() - 8 * 86400000).toISOString() },
  { id: '2', service: 'payment-service', incident_type: 'latency_spike', failure_signature: 'high p99 latency on /checkout', resolution: 'rollback to v2.3.1', ttresolve_seconds: 1240, outcome: 'resolved', confidence: 0.74, occurred_at: new Date(Date.now() - 22 * 86400000).toISOString() },
  { id: '3', service: 'payment-service', incident_type: 'error_rate', failure_signature: 'stripe API rate limit hit', resolution: 'implement exponential backoff', ttresolve_seconds: 180, outcome: 'escalated', confidence: 0.91, occurred_at: new Date(Date.now() - 45 * 86400000).toISOString() },
]

export function EpisodeMemory({ service, incidentType, compact }: EpisodeMemoryProps) {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const params = new URLSearchParams()
    if (service) params.set('service', service)
    if (incidentType) params.set('incident_type', incidentType)
    params.set('limit', '3')

    fetch(`/api/memory/episodes?${params}`)
      .then(r => r.ok ? r.json() : null)
      .then((data: unknown) => {
        if (data && typeof data === 'object' && 'episodes' in data) {
          const d = data as { episodes: unknown }
          if (Array.isArray(d.episodes)) {
            setEpisodes(d.episodes as Episode[])
            return
          }
        }
        setEpisodes(DEMO_EPISODES)
      })
      .catch(() => setEpisodes(DEMO_EPISODES))
      .finally(() => setLoading(false))
  }, [service, incidentType])

  if (loading) {
    return <div className="text-slate-500 text-xs px-3 py-4">Loading episodes...</div>
  }

  if (episodes.length === 0) {
    return <div className="text-slate-500 text-xs px-3 py-4 text-center">No similar episodes recorded yet</div>
  }

  if (compact) {
    return (
      <div className="space-y-1.5">
        {episodes.map(ep => (
          <div key={ep.id} className="flex items-center gap-2 px-3 py-1.5 bg-slate-900/60 rounded border border-slate-800/50">
            <span className="text-xs">🔴</span>
            <span className="text-slate-300 text-xs truncate flex-1">{ep.failure_signature}</span>
            <span className="text-slate-500 text-[10px] shrink-0">{formatDuration(ep.ttresolve_seconds)}</span>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <Clock size={13} className="text-slate-500"/>
        <span className="text-slate-300 text-xs font-semibold">Past Episodes{service ? ` — ${service}` : ''}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-800 text-slate-400 border border-slate-700">{episodes.length}</span>
      </div>
      <div className="space-y-2">
        {episodes.map(ep => (
          <div key={ep.id} className="bg-slate-900/60 border border-slate-800/60 rounded-md overflow-hidden">
            <div className="px-3 py-2 flex items-center gap-2">
              <span className="text-xs">🔴</span>
              <span className="text-slate-300 text-xs font-medium truncate">{ep.service}</span>
              <span className="text-slate-500 text-[10px]">·</span>
              <span className="text-slate-500 text-[10px]">{ep.incident_type}</span>
              <span className="text-slate-600 text-[10px] ml-auto shrink-0">{timeAgo(ep.occurred_at)}</span>
            </div>
            <div className="px-3 pb-2">
              <div className="text-slate-400 text-xs italic mb-1.5">&quot;{ep.failure_signature}&quot;</div>
              <div className="border-t border-slate-800/60 pt-1.5 space-y-0.5">
                <div className="text-slate-500 text-[10px]">Resolution: <span className="text-slate-400">{ep.resolution}</span></div>
                <div className="flex items-center gap-2">
                  <span className="text-slate-500 text-[10px]">TTR: <span className="text-slate-400">{formatDuration(ep.ttresolve_seconds)}</span></span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border ${outcomeColor(ep.outcome)}`}>{ep.outcome}</span>
                  <span className="text-slate-500 text-[10px] ml-auto">Confidence: <span className="text-slate-400">{Math.round(ep.confidence * 100)}%</span></span>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
