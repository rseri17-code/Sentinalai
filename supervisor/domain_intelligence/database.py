"""Database Intelligence Module (Phase 6 DIE, first module).

Owns the investigative expertise for the database domain: it interprets universal
database failure signatures (deadlock victims, connection-pool timeouts, query-plan
regressions to sequential scans, read-replica lag) and emits canonical root-cause
hypotheses that flow through the engine's existing confidence/elimination machinery.

Signatures are standard production database markers — HikariPool timeouts, "deadlock
victim", "seq scan" plans, "read-after-write" / replica lag — not EFIC strings, so
the module generalizes to any real database incident of these modes. Flag-gated by
``DI_DATABASE_ENABLED`` (default off).
"""
from __future__ import annotations

from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class DatabaseIntelligence(DomainModule):
    name = "database"
    flag_env = "DI_DATABASE_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []

        # --- deadlock: lock-order inversion / deadlock victim ---
        if "deadlock" in text:
            hyps.append(_hyp(
                "database_deadlock",
                f"database deadlock from lock-order inversion causing failed "
                f"transactions in {svc}",
                82, ["logs:deadlock_victim", "database:deadlocks"],
                "Database reports deadlock victims / rolled-back transactions — "
                "two code paths acquiring locks in opposite order.",
                "normalize lock acquisition order; add a bounded deadlock retry"))

        # --- connection pool exhaustion: pool timeouts / active==max ---
        active = _num(view.metric_value("active"))
        pool_max = _num(view.metric_value("max"))
        pool_saturated = active is not None and pool_max is not None and active >= pool_max > 0
        if ("connection pool" in text or "hikaripool" in text or "pool timeout" in text
                or ("pool" in text and "timeout" in text) or pool_saturated):
            hyps.append(_hyp(
                "database_pool_exhaustion",
                f"database connection pool exhaustion from unclosed connections in {svc}",
                82, ["logs:pool_timeout", "database:active_at_max"],
                "Connection-pool acquisition timeouts with active connections at the "
                "pool maximum — a connection leak or under-sized pool under load.",
                "fix the connection leak; size the pool to peak load"))

        # --- slow query: query-plan regression to a full/sequential scan ---
        if ("seq scan" in text or "sequential scan" in text or "full table scan" in text
                or "full scan" in text or ("plan" in text and "scan" in text)):
            hyps.append(_hyp(
                "database_slow_query",
                f"database slow query from a query plan regression (full table scan) "
                f"in {svc}",
                80, ["database:query_plan_regression"],
                "The optimizer flipped to a sequential/full-table scan (e.g. after a "
                "statistics refresh), collapsing query latency.",
                "pin or refresh the query plan; add a plan-regression guard"))

        # --- replica lag: stale reads / read-after-write mismatch ---
        if ("read-after-write" in text or "replication lag" in text
                or ("replica" in text and ("lag" in text or "stale" in text))):
            hyps.append(_hyp(
                "database_replica_lag",
                f"database read replica lag serving stale reads in {svc}",
                80, ["database:replica_lag", "logs:read_after_write"],
                "Read-after-write mismatches consistent with a lagging read replica "
                "(e.g. reads routed to a replica during a bulk write).",
                "route critical reads to the primary during bulk jobs; alert on lag"))

        return hyps


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
