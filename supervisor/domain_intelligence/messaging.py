"""Messaging Intelligence Module (Phase 6 DIE). Kafka consumer lag from a poison
message. Flag-gated by DI_MESSAGING_ENABLED (default off)."""
from __future__ import annotations
from supervisor.domain_intelligence.base import DomainModule, EvidenceView, _hyp


class MessagingIntelligence(DomainModule):
    name = "messaging"
    flag_env = "DI_MESSAGING_ENABLED"

    def analyze(self, view: EvidenceView) -> list[dict]:
        text = view.text
        svc = view.service
        hyps: list[dict] = []
        # Kafka consumer stuck (paused) on a poison message -> lag grows.
        if ("consumer" in text and ("paused" in text or "lag" in text
                                    or "deserial" in text)) or "poison" in text:
            hyps.append(_hyp(
                "kafka_consumer_lag_poison",
                f"kafka consumer lag: the {svc} consumer is stuck (paused) on a "
                f"poison message (deserialization error), so lag grows",
                82, ["logs:consumer_paused", "logs:deserialization_error"],
                "A malformed (poison) message paused the consumer with no dead-letter "
                "handling, so consumer lag climbs while the broker stays healthy.",
                "add a dead-letter queue; skip+alert on poison messages"))

        # Redis maxmemory eviction -> cache-miss stampede onto the backing DB.
        # (redis_evicted_keys + a cache-hit-ratio collapse are the APM signals.)
        if ("redis" in text and ("evict" in text or "maxmemory" in text)) or (
                "evicted_keys" in text or (
                    "cache_hit" in text and ("0.4" in text or "stampede" in text
                                             or "->" in text))):
            hyps.append(_hyp(
                "redis_eviction_stampede",
                f"Redis maxmemory eviction caused a cache-miss stampede onto the "
                f"{svc} database (cache hit ratio collapsed, DB QPS spiked)",
                82, ["metrics:redis_evicted_keys", "metrics:cache_hit_drop"],
                "Redis evicted keys under memory pressure; the cache-hit ratio "
                "collapsed and the resulting cache-miss stampede overloaded the "
                "backing database (the DB load is the symptom, eviction the cause).",
                "raise Redis maxmemory / tune eviction; add request coalescing"))
        return hyps
