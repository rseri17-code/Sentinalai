// MTTI — Mean Time to Identify
// = time from investigation.started → hypothesis.selected OR rca.generated
// MTTI measures agent cognition speed; MTTR measures full resolution cycle.

import type { AGUIEvent, IncidentState } from '@/types'

/** Returns MTTI in milliseconds, or null if root cause not yet identified. */
export function computeMTTI(
  events: AGUIEvent[],
  investigation: IncidentState | null
): number | null {
  if (!investigation?.started_at || events.length === 0) return null
  const identifiedAt = (
    events.find((e) => e.event_type === 'hypothesis.selected') ??
    events.find((e) => e.event_type === 'rca.generated')
  )?.timestamp
  if (!identifiedAt) return null
  return new Date(identifiedAt).getTime() - new Date(investigation.started_at).getTime()
}

/** Format ms as "4m 12s", "38s", or "1h 2m" */
export function fmtDuration(ms: number): string {
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  if (ms < 3_600_000) {
    const m = Math.floor(ms / 60_000)
    const s = Math.round((ms % 60_000) / 1000)
    return s ? `${m}m ${s}s` : `${m}m`
  }
  const h = Math.floor(ms / 3_600_000)
  const m = Math.round((ms % 3_600_000) / 60_000)
  return m ? `${h}h ${m}m` : `${h}h`
}

/** Elapsed ms since a UTC timestamp string */
export function elapsedSince(isoString: string): number {
  return Date.now() - new Date(isoString).getTime()
}
