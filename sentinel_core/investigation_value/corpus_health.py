"""P3 — Corpus Intelligence: nightly health detection.

Treats the memory corpus as a production knowledge system. Detects,
deterministically and offline:

  duplicates            same fingerprint, multiple records
  conflicts             same fingerprint, materially different causes
  stale records         older than the staleness window
  obsolete services     services absent from the recent window
  schema drift          schema_version distribution
  confidence drift      recent-vs-prior mean confidence delta
  quality regression    recent-vs-prior mean investigation_score delta

All time comparisons are lexicographic ISO-8601 against caller-supplied
``as_of`` (never ``now()`` — determinism rule). Reports only; the corpus
is immutable — health findings become sidecar evidence, never edits.
"""
from __future__ import annotations

from typing import Any, Iterable

from sentinel_core.investigation_value.metrics import _jaccard, _tokens

CORPUS_HEALTH_SCHEMA_VERSION = 1

# ISO-8601 duration windows expressed as day counts for lex comparison.
STALE_DAYS = 90
RECENT_DAYS = 30
# Same-fingerprint causes below this token overlap conflict.
CONFLICT_JACCARD = 0.30


def _day_shift(as_of: str, days: int) -> str:
    """ISO date minus N days — pure arithmetic on the date prefix."""
    import datetime as _dt
    base = _dt.date.fromisoformat(str(as_of)[:10])
    return (base - _dt.timedelta(days=days)).isoformat()


def corpus_health_report(
    records: Iterable[Any], as_of: str,
) -> dict[str, Any]:
    recs = sorted(records, key=lambda r: r.memory_id)
    stale_cutoff = _day_shift(as_of, STALE_DAYS)
    recent_cutoff = _day_shift(as_of, RECENT_DAYS)

    by_fp: dict[str, list[Any]] = {}
    for r in recs:
        if r.fingerprint:
            by_fp.setdefault(r.fingerprint, []).append(r)

    duplicates = sorted(
        fp for fp, group in by_fp.items() if len(group) > 1
    )
    conflicts = []
    for fp in duplicates:
        group = by_fp[fp]
        causes = [(r.memory_id, _tokens(r.detected_root_cause))
                  for r in group]
        for i in range(len(causes)):
            for j in range(i + 1, len(causes)):
                if causes[i][1] and causes[j][1] and \
                        _jaccard(causes[i][1], causes[j][1]) \
                        < CONFLICT_JACCARD:
                    conflicts.append({
                        "fingerprint": fp,
                        "memory_ids": sorted(
                            [causes[i][0], causes[j][0]]),
                    })
    conflicts.sort(key=lambda c: (c["fingerprint"],
                                    tuple(c["memory_ids"])))

    stale = sorted(r.memory_id for r in recs
                    if r.timestamp and str(r.timestamp)[:10] < stale_cutoff)

    recent = [r for r in recs
              if r.timestamp and str(r.timestamp)[:10] >= recent_cutoff]
    prior = [r for r in recs
             if r.timestamp and str(r.timestamp)[:10] < recent_cutoff]
    recent_services = {r.service for r in recent if r.service}
    prior_services = {r.service for r in prior if r.service}
    obsolete_services = sorted(prior_services - recent_services)

    versions: dict[str, int] = {}
    for r in recs:
        key = str(r.schema_version)
        versions[key] = versions.get(key, 0) + 1

    def _mean(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 4) if vals else None

    conf_recent = _mean([float(r.confidence) for r in recent])
    conf_prior = _mean([float(r.confidence) for r in prior])
    qual_recent = _mean([float(r.investigation_score) for r in recent])
    qual_prior = _mean([float(r.investigation_score) for r in prior])

    return {
        "schema_version": CORPUS_HEALTH_SCHEMA_VERSION,
        "as_of": str(as_of),
        "record_count": len(recs),
        "duplicates": {"fingerprints": duplicates,
                        "count": len(duplicates)},
        "conflicts": conflicts,
        "stale": {"cutoff": stale_cutoff, "memory_ids": stale,
                   "count": len(stale)},
        "obsolete_services": obsolete_services,
        "schema_versions": {k: versions[k] for k in sorted(versions)},
        "confidence_drift": {
            "recent_mean": conf_recent, "prior_mean": conf_prior,
            "delta": (round(conf_recent - conf_prior, 4)
                       if conf_recent is not None and conf_prior is not None
                       else None),
        },
        "quality_drift": {
            "recent_mean": qual_recent, "prior_mean": qual_prior,
            "delta": (round(qual_recent - qual_prior, 4)
                       if qual_recent is not None and qual_prior is not None
                       else None),
        },
    }


__all__ = ["CORPUS_HEALTH_SCHEMA_VERSION", "corpus_health_report"]
