"""
Expected RCA outputs for test incidents.
These define PRODUCTION QUALITY - code must match these to pass tests.
"""

EXPECTED_RCA = {
    # =========================================================================
    # INC12345 - Timeout Incident
    # =========================================================================
    "INC12345": {
        "incident_id": "INC12345",
        "root_cause": "payment-service database slow queries",
        "root_cause_keywords": ["payment-service", "database", "slow", "queries"],
        "confidence_min": 90,
        "confidence_max": 100,
        "required_evidence": [
            "payment-service latency spike",
            "api-gateway timeout errors",
            "latency preceded timeouts",
        ],
        "timeline_correctness": {
            "must_have_events": [
                {
                    "event_pattern": "latency|slow response",
                    "service": "payment-service",
                    "time_window": "2024-02-12T10:30:10Z to 10:30:12Z",
                },
                {
                    "event_pattern": "timeout",
                    "service": "api-gateway",
                    "time_window": "2024-02-12T10:30:15Z to 10:30:20Z",
                },
            ],
            "first_event_must_be": "payment-service latency",
            "causal_chain_required": True,
            "causal_chain_pattern": "latency.*timeout|slow.*timeout",
        },
        "reasoning_requirements": {
            "must_explain_causality": True,
            "must_mention_timeline": True,
            "must_identify_first_fault": True,
            "keywords_required": ["latency", "preceded", "caused", "downstream"],
        },
        "investigation_time_max_seconds": 60,
        "tools_that_must_be_called": [
            "moogsoft.get_incident_by_id",
            "splunk.search_oneshot",
            "sysdig.golden_signals",
        ],
        "tools_that_should_not_be_called": [
            "splunk.get_indexes",
            "splunk.get_config",
            "sysdig.discover_resources",
        ],
    },
    # =========================================================================
    # INC12346 - OOMKill Incident
    # =========================================================================
    "INC12346": {
        "incident_id": "INC12346",
        "root_cause": "memory leak in user-service",
        "root_cause_keywords": ["memory", "leak", "user-service"],
        "confidence_min": 85,
        "confidence_max": 92,
        "required_evidence": [
            "OOMKill event",
            "gradual memory increase",
            "memory saturation",
        ],
        "timeline_correctness": {
            "must_show_gradual_increase": True,
            "pattern": "memory increases over time then OOMKill",
            "time_span_hours": 1.5,
        },
        "reasoning_requirements": {
            "must_explain_pattern": "gradual increase indicates leak",
            "must_mention_oomkill": True,
        },
        "investigation_time_max_seconds": 60,
    },
    # =========================================================================
    # INC12347 - Error Spike After Deployment
    # =========================================================================
    "INC12347": {
        "incident_id": "INC12347",
        "root_cause": "deployment v3.1.0 introduced NullPointerException",
        "root_cause_keywords": ["deployment", "v3.1.0", "NullPointerException"],
        "confidence_min": 88,
        "confidence_max": 95,
        "required_evidence": [
            "deployment occurred",
            "errors started after deployment",
            "NullPointerException",
        ],
        "timeline_correctness": {
            "deployment_must_precede_errors": True,
            "time_gap_max_seconds": 30,
        },
        "reasoning_requirements": {
            "must_correlate_deployment": True,
            "must_mention_error_type": True,
        },
        "investigation_time_max_seconds": 60,
        "change_correlation_required": True,
    },
    # =========================================================================
    # INC12348 - Latency Incident
    # =========================================================================
    "INC12348": {
        "incident_id": "INC12348",
        "root_cause": "Elasticsearch cluster rebalancing causing slow queries in search-service",
        "root_cause_keywords": ["elasticsearch", "rebalancing", "search-service"],
        "confidence_min": 85,
        "confidence_max": 100,
        "required_evidence": [
            "search-service latency spike",
            "elasticsearch rebalancing event",
            "slow query logs",
        ],
        "timeline_correctness": {
            "first_event_must_be": "elasticsearch rebalancing",
            "causal_chain_required": True,
        },
        "reasoning_requirements": {
            "must_explain_causality": True,
            "must_mention_backend": True,
        },
        "investigation_time_max_seconds": 60,
    },
    # =========================================================================
    # INC12349 - Resource Saturation
    # =========================================================================
    "INC12349": {
        "incident_id": "INC12349",
        "root_cause": "infinite loop in order-service validation after config change",
        "root_cause_keywords": ["order-service", "cpu", "config"],
        "confidence_min": 85,
        "confidence_max": 100,
        "required_evidence": [
            "CPU saturation at 99%",
            "config change preceded CPU spike",
            "thread pool exhaustion",
        ],
        "timeline_correctness": {
            "config_change_must_precede_cpu_spike": True,
            "time_gap_max_seconds": 10,
        },
        "reasoning_requirements": {
            "must_correlate_config_change": True,
            "must_explain_cpu_spike": True,
        },
        "investigation_time_max_seconds": 60,
        "change_correlation_required": True,
    },
    # =========================================================================
    # INC12350 - Network Issue
    # =========================================================================
    "INC12350": {
        "incident_id": "INC12350",
        "root_cause": "DNS resolution failure after DNS server maintenance",
        "root_cause_keywords": ["dns", "resolution", "maintenance"],
        "confidence_min": 88,
        "confidence_max": 100,
        "required_evidence": [
            "DNS maintenance event",
            "connection refused errors across multiple services",
            "dns resolution failure",
        ],
        "timeline_correctness": {
            "maintenance_must_precede_failures": True,
            "must_show_multi_service_impact": True,
        },
        "reasoning_requirements": {
            "must_identify_infrastructure_cause": True,
            "must_explain_broad_impact": True,
        },
        "investigation_time_max_seconds": 60,
        "change_correlation_required": True,
    },
    # =========================================================================
    # INC12351 - Complex Multi-Cause Incident
    # =========================================================================
    "INC12351": {
        "incident_id": "INC12351",
        "root_cause": "database connection pool exhaustion in payment-service caused by slow queries after index drop",
        "root_cause_keywords": ["connection pool", "payment-service", "index"],
        "confidence_min": 80,
        "confidence_max": 95,
        "required_evidence": [
            "database slow queries",
            "connection pool exhaustion",
            "cascading failures to checkout and gateway",
        ],
        "timeline_correctness": {
            "must_show_cascade": True,
            "cascade_order": [
                "payment-db slow queries",
                "payment-service pool exhaustion",
                "checkout-service failures",
                "api-gateway timeouts",
            ],
        },
        "reasoning_requirements": {
            "must_explain_cascade": True,
            "must_identify_root_trigger": True,
        },
        "investigation_time_max_seconds": 60,
        "change_correlation_required": True,
    },
    # =========================================================================
    # INC12352 - Missing Data Scenario
    # =========================================================================
    "INC12352": {
        "incident_id": "INC12352",
        "root_cause": "Redis connection failure affecting notification-service",
        "root_cause_keywords": ["redis", "connection", "notification-service"],
        "confidence_min": 60,
        "confidence_max": 88,
        "required_evidence": [
            "Redis connection refused",
        ],
        "timeline_correctness": {
            "must_handle_missing_metrics": True,
        },
        "reasoning_requirements": {
            "must_acknowledge_limited_data": True,
        },
        "investigation_time_max_seconds": 60,
        "allows_lower_confidence": True,
    },
    # =========================================================================
    # INC12353 - Flapping Alerts
    # =========================================================================
    "INC12353": {
        "incident_id": "INC12353",
        "root_cause": "connection pool leak in auth-service causing intermittent exhaustion",
        "root_cause_keywords": ["connection pool", "auth-service", "intermittent"],
        "confidence_min": 75,
        "confidence_max": 90,
        "required_evidence": [
            "intermittent connection pool exhaustion",
            "sawtooth pattern in connections",
        ],
        "timeline_correctness": {
            "must_show_pattern": True,
            "pattern_type": "sawtooth",
        },
        "reasoning_requirements": {
            "must_identify_pattern": True,
            "must_explain_intermittent_nature": True,
        },
        "investigation_time_max_seconds": 60,
    },
    # =========================================================================
    # INC12354 - Silent Failure
    # =========================================================================
    "INC12354": {
        "incident_id": "INC12354",
        "root_cause": "data pipeline failure causing stale cache in recommendation-service",
        "root_cause_keywords": ["pipeline", "stale", "recommendation-service"],
        "confidence_min": 78,
        "confidence_max": 92,
        "required_evidence": [
            "data pipeline failure",
            "stale cache",
            "throughput drop",
        ],
        "timeline_correctness": {
            "pipeline_must_precede_throughput_drop": True,
            "must_show_gradual_degradation": True,
        },
        "reasoning_requirements": {
            "must_explain_indirect_cause": True,
            "must_identify_upstream_failure": True,
        },
        "investigation_time_max_seconds": 60,
    },
    # INC12355 - K8s CrashLoopBackOff: cart-service pods restarting
    "INC12355": {
        "incident_id": "INC12355",
        "root_cause": "ConfigMap REDIS_HOST key renamed causing readiness probe failure and CrashLoopBackOff",
        "root_cause_keywords": ['error spike', 'cart-service'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['CrashLoopBackOff event', 'readiness probe failed', 'ConfigMap change 3 minutes before'],
        "timeline_correctness": {'configmap_change_must_precede_errors': True},
        "reasoning_requirements": {'must_correlate_config_change': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12356 - K8s node eviction storm: production cluster
    "INC12356": {
        "incident_id": "INC12356",
        "root_cause": "Node memory pressure caused eviction cascade across 12 pods rescheduling thrash",
        "root_cause_keywords": ['error spike', 'checkout-service'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['NodeMemoryPressure event', 'pod evictions across 12 pods', 'rescheduling contention'],
        "timeline_correctness": {'must_show_cascade': True},
        "reasoning_requirements": {'must_identify_node_pressure': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12357 - TLS certificate expired: api.payments.internal
    "INC12357": {
        "incident_id": "INC12357",
        "root_cause": "TLS certificate for api.payments.internal expired at midnight causing SSL handshake failures",
        "root_cause_keywords": ['connectivity', 'payment-service'],
        "confidence_min": 48,
        "confidence_max": 100,
        "required_evidence": ['certificate expired log', 'x509 expiry error', 'errors started at midnight'],
        "timeline_correctness": {'expiry_must_precede_errors': True},
        "reasoning_requirements": {'must_identify_cert_expiry': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12358 - Disk I/O saturation: order-db IOPS exhausted
    "INC12358": {
        "incident_id": "INC12358",
        "root_cause": "Analytics full-table scan exhausted disk IOPS on order-db node causing io_wait 94%",
        "root_cause_keywords": ['order-service', 'saturation'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['io_wait 94%', 'disk read latency 2800ms', 'analytics query full-table scan'],
        "timeline_correctness": {'analytics_job_must_precede_saturation': True},
        "reasoning_requirements": {'must_identify_iops_exhaustion': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12359 - NTP clock skew: distributed lock TTL broken
    "INC12359": {
        "incident_id": "INC12359",
        "root_cause": "NTP server unreachable caused 4735ms clock drift breaking distributed lock TTLs",
        "root_cause_keywords": ["inventory-service"],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['clock skew 4735ms', 'lock expired prematurely', 'consensus timeout'],
        "timeline_correctness": {'ntp_failure_must_precede_lock_failures': True},
        "reasoning_requirements": {'must_identify_clock_skew': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12360 - AWS S3 partial degradation: media uploads failing
    "INC12360": {
        "incident_id": "INC12360",
        "root_cause": "AWS S3 us-east-1 partial outage causing media upload timeouts cascading to CDN failures",
        "root_cause_keywords": ['media-service', 'degraded'],
        "confidence_min": 30,
        "confidence_max": 95,
        "required_evidence": ['S3PutObjectError RequestTimeout', 'CDN origin fetch failed', 'AWS health event S3 degraded'],
        "timeline_correctness": {'s3_degradation_must_precede_cdn_failures': True},
        "reasoning_requirements": {'must_identify_external_dependency': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12361 - File descriptor leak: api-gateway FD exhaustion
    "INC12361": {
        "incident_id": "INC12361",
        "root_cause": "HTTP keep-alive connection leak caused gradual fd exhaustion over 6 hours collapsing connection pool",
        "root_cause_keywords": ['api-gateway', 'saturation'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['fd count 65280/65536', 'too many open files', 'gradual growth over 6 hours'],
        "timeline_correctness": {'fd_growth_must_be_gradual': True},
        "reasoning_requirements": {'must_identify_fd_leak': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12362 - PostgreSQL replication lag: read replicas 45min behind
    "INC12362": {
        "incident_id": "INC12362",
        "root_cause": "VACUUM FREEZE on primary blocked WAL archiving for 2743 seconds causing 45min replica lag",
        "root_cause_keywords": ['error spike', 'reporting-service'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['replication lag 2743 seconds', 'WAL sender waiting', 'stale reads on replica'],
        "timeline_correctness": {'vacuum_must_precede_replica_lag': True},
        "reasoning_requirements": {'must_identify_wal_blocking': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12363 - Postgres autovacuum blocked: table bloat causing full scans
    "INC12363": {
        "incident_id": "INC12363",
        "root_cause": "Long-running OLAP transaction blocked autovacuum 3h causing dead tuple ratio 0.78 and seq scan fallback",
        "root_cause_keywords": ['error spike', 'analytics-service'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['847293 dead tuples', 'query plan switched to SeqScan', 'query 127s vs 0.3s baseline'],
        "timeline_correctness": {'blocking_tx_must_precede_bloat': True},
        "reasoning_requirements": {'must_identify_autovacuum_block': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12364 - Redis eviction storm: maxmemory allkeys-lru evicting sessions
    "INC12364": {
        "incident_id": "INC12364",
        "root_cause": "Redis hit maxmemory limit evicting 847293 active session keys causing mass re-auth storm on auth-service",
        "root_cause_keywords": ['error spike', 'session-service'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['Redis memory 100%', 'evicted_keys 847293', 'cache hit rate dropped to 6%', 'auth-service 70x rps'],
        "timeline_correctness": {'redis_full_must_precede_eviction': True},
        "reasoning_requirements": {'must_identify_cache_eviction_storm': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12365 - JVM thread deadlock: payment-processor zero throughput
    "INC12365": {
        "incident_id": "INC12365",
        "root_cause": "Deadlock between TransactionManager and InventoryLock threads causing zero throughput with low CPU",
        "root_cause_keywords": ['throughput', 'payment-processor'],
        "confidence_min": 48,
        "confidence_max": 100,
        "required_evidence": ['Java-level deadlock found', 'TransactionManager waiting for InventoryLock', 'throughput 0 tps'],
        "timeline_correctness": {'deadlock_causes_zero_throughput': True},
        "reasoning_requirements": {'must_identify_thread_deadlock': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12366 - Goroutine leak: notification-service memory growing
    "INC12366": {
        "incident_id": "INC12366",
        "root_cause": "Webhook retry goroutines not cancelled on context timeout accumulating at 500/min causing eventual OOM",
        "root_cause_keywords": ['error spike', 'notification-service'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['goroutine count 47293 vs baseline 120', 'GC pause 2347ms', 'throughput 0 silent'],
        "timeline_correctness": {'goroutine_growth_must_be_gradual': True},
        "reasoning_requirements": {'must_identify_goroutine_leak': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12367 - Feature flag regression: dark-launch-v2 rolled to 100%
    "INC12367": {
        "incident_id": "INC12367",
        "root_cause": "LaunchDarkly flag dark-launch-v2 accidentally set to 100% enabling broken GBP payment path",
        "root_cause_keywords": ['error spike', 'checkout-service'],
        "confidence_min": 50,
        "confidence_max": 100,
        "required_evidence": ['flag dark-launch-v2=true', 'PaymentPathV2Error unsupported currency GBP', 'error rate 23%'],
        "timeline_correctness": {'flag_change_must_precede_errors': True},
        "reasoning_requirements": {'must_correlate_flag_change': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12368 - Transitive dependency: guava version conflict NoSuchMethodError
    "INC12368": {
        "incident_id": "INC12368",
        "root_cause": "data-service v4.2.0 pulled guava 32.1.3 conflicting with spring-boot shaded guava 20.0 causing NoSuchMethodError",
        "root_cause_keywords": ['error spike', 'data-service'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['NoSuchMethodError ImmutableList.toImmutableList', 'multiple guava versions detected', 'deployment v4.2.0 3min before'],
        "timeline_correctness": {'deployment_must_precede_errors': True},
        "reasoning_requirements": {'must_identify_classpath_conflict': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12369 - Avro schema mismatch: producer v7 consumer still on v6
    "INC12369": {
        "incident_id": "INC12369",
        "root_cause": "Producer deployed Avro schema v7 with required field correlation_id breaking v6 consumers",
        "root_cause_keywords": ['error spike', 'event-consumer'],
        "confidence_min": 50,
        "confidence_max": 100,
        "required_evidence": ['AvroTypeException Expected field correlation_id', 'schema ID 142 incompatible with 138', 'producer deployed 8min before'],
        "timeline_correctness": {'producer_deploy_must_precede_consumer_errors': True},
        "reasoning_requirements": {'must_identify_schema_incompatibility': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12370 - BGP route flap: packet loss bursts every 4 minutes
    "INC12370": {
        "incident_id": "INC12370",
        "root_cause": "BGP peer 203.0.113.1 hold timer expiry causing rhythmic 12-second packet loss bursts every 4 minutes",
        "root_cause_keywords": ['error spike', 'edge-router'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['BGP peer went down', '34% packet loss on AS65001', 'rhythmic 4-minute loss bursts'],
        "timeline_correctness": {'flap_pattern_must_be_rhythmic': True},
        "reasoning_requirements": {'must_identify_bgp_flap': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12371 - DDoS traffic flood: checkout endpoint 847K rps
    "INC12371": {
        "incident_id": "INC12371",
        "root_cause": "Volumetric DDoS attack 847K rps from 3 ASes saturating checkout-service collateral 94% user error rate",
        "root_cause_keywords": ['error spike', 'checkout-service'],
        "confidence_min": 50,
        "confidence_max": 100,
        "required_evidence": ['rate limit exceeded 847293 rps', 'SYN flood detected', 'legitimate user error rate 94%'],
        "timeline_correctness": {'attack_traffic_must_dominate': True},
        "reasoning_requirements": {'must_identify_volumetric_attack': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12372 - Istio mTLS cert rotation: 3 services missed CA update
    "INC12372": {
        "incident_id": "INC12372",
        "root_cause": "Istio root CA rotation updated cluster-wide but 3 service accounts kept stale SPIFFE certs breaking mTLS",
        "root_cause_keywords": ['network connectivity', 'service-mesh'],
        "confidence_min": 48,
        "confidence_max": 98,
        "required_evidence": ['x509 certificate signed by unknown authority', '503 upstream connection failure', '3 service pairs affected'],
        "timeline_correctness": {'ca_rotation_must_precede_mtls_failures': True},
        "reasoning_requirements": {'must_identify_stale_cert_after_rotation': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12373 - Rate limiter misconfiguration: internal services throttled at 100rps
    "INC12373": {
        "incident_id": "INC12373",
        "root_cause": "Rate limiter config v2.3.1 applied user-tier limits 100rps to internal service accounts needing 10K rps",
        "root_cause_keywords": ['error spike', 'api-gateway'],
        "confidence_min": 50,
        "confidence_max": 100,
        "required_evidence": ['429 rate limit exceeded for service-account-internal', 'X-RateLimit-Limit: 100', 'config v2.3.1 deployed 5min before'],
        "timeline_correctness": {'config_change_must_precede_throttling': True},
        "reasoning_requirements": {'must_correlate_config_change': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12374 - Thundering herd: nightly cache flush saturated product-db
    "INC12374": {
        "incident_id": "INC12374",
        "root_cause": "Nightly cache FLUSH ALL at 03:00 caused 8000 simultaneous cache misses exhausting HikariCP pool in 2 seconds",
        "root_cause_keywords": ['error spike', 'product-service'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['Cache FLUSH ALL 847293 keys', 'HikariCP connection timeout', 'DB connections 0 to 500 in 2 seconds', 'exactly 03:00'],
        "timeline_correctness": {'cache_flush_must_precede_db_saturation': True},
        "reasoning_requirements": {'must_identify_thundering_herd': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12375 - Blue/green stuck at 50%: recommendation-service v2.4.1 broken
    "INC12375": {
        "incident_id": "INC12375",
        "root_cause": "Argo Rollouts canary analysis failed success-rate 0.67 pausing rollout at 50% leaving half users on broken v2.4.1",
        "root_cause_keywords": ['error spike', 'recommendation-service'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['CanaryAnalysis FAILED success-rate 0.67', 'rollout paused at weight=50%', 'v2.4.1 NullPointerException'],
        "timeline_correctness": {'canary_failure_must_cause_split_traffic': True},
        "reasoning_requirements": {'must_identify_stuck_rollout': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12376 - Kafka consumer lag: fraud-detection stalled on poison pill
    "INC12376": {
        "incident_id": "INC12376",
        "root_cause": "Deserialization error on poison-pill message at offset 2847293 caused MaxPollIntervalExceededException stalling consumer silently",
        "root_cause_keywords": ['error spike', 'fraud-detection'],
        "confidence_min": 50,
        "confidence_max": 98,
        "required_evidence": ['consumer lag 2847293 growing 10000/min', 'JsonParseException at offset 2847293', 'consumer throughput 0 silent'],
        "timeline_correctness": {'poison_pill_must_precede_lag_growth': True},
        "reasoning_requirements": {'must_identify_poison_pill': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12377 - Noisy neighbor: analytics job starving billing-service on shared RDS
    "INC12377": {
        "incident_id": "INC12377",
        "root_cause": "Analytics ETL full-table scan SELECT * FROM events consuming 94% shared RDS CPU starving billing-service queries",
        "root_cause_keywords": ['error spike', 'billing-service'],
        "confidence_min": 50,
        "confidence_max": 95,
        "required_evidence": ['RDS CPU 94%', 'billing-service query timeout 30s', 'SELECT * FROM events 847M rows no WHERE clause'],
        "timeline_correctness": {'analytics_job_must_coincide_with_billing_errors': True},
        "reasoning_requirements": {'must_identify_noisy_neighbor': True},
        "investigation_time_max_seconds": 60,
    },
    # INC12378 - Third-party API degradation: Stripe elevated latency eu-west-1
    "INC12378": {
        "incident_id": "INC12378",
        "root_cause": "Stripe API experiencing elevated latency p99 28347ms vs normal 200ms in eu-west-1 causing payment timeouts",
        "root_cause_keywords": ['payment-service', 'latency'],
        "confidence_min": 55,
        "confidence_max": 98,
        "required_evidence": ['Stripe API timeout 28347ms', 'APIConnectionError max retries', 'Stripe status page degraded eu-west-1'],
        "timeline_correctness": {'stripe_latency_must_precede_payment_failures': True},
        "reasoning_requirements": {'must_identify_external_vendor_degradation': True},
        "investigation_time_max_seconds": 60,
    },
}
