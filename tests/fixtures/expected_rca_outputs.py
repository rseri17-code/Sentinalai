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
        # Three in-window raw latency points. The golden-signals summary is
        # derived and is not scored. 34 + 8 + 8. The cause stays UNKNOWN.
        "root_cause": "payment-service latency elevated; cause UNKNOWN",
        "root_cause_keywords": ["payment-service", "latency", "elevated", "unknown"],
        "confidence_min": 50,
        "confidence_max": 50,
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
        "root_cause": "OOMKill; memory usage increased in user-service",
        "root_cause_keywords": ["memory", "oom", "user-service"],
        # OOM log, OOM event, and two in-window memory points. 62 + 8*3.
        # Points before 14:07:33 are outside the window and are not scored.
        "confidence_min": 86,
        "confidence_max": 86,
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
        "root_cause": "NullPointerException in payment-service v3.1.0, deployed at 2024-02-12T09:00:00Z",
        "root_cause_keywords": ["deployed", "v3.1.0", "NullPointerException"],
        # C1. The exception log plus the change record, which names
        # payment-service. 62 + 8 = 70. The deploy event has no service
        # or CI field, so it is not a third ref (that was 78). It is
        # "change in window, service not identified".
        "confidence_min": 70,
        "confidence_max": 70,
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
        "root_cause": "slow queries on elasticsearch in search-service",
        "root_cause_keywords": ["elasticsearch", "slow", "search-service"],
        # Two in-window slow-query lines. 62 + 8.
        "confidence_min": 70,
        "confidence_max": 70,
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
        "root_cause": "cpu exhaustion; thread pool saturation; config change in order-service",
        "root_cause_keywords": ["order-service", "cpu", "config"],
        # Four cpu points, two thread-pool lines, and the config change.
        # 62 + 8*6 = 110, capped at 90.
        "confidence_min": 90,
        "confidence_max": 90,
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
        "root_cause": "dns resolution failure; maintenance; dns in inventory-service",
        "root_cause_keywords": ["dns", "resolution", "maintenance"],
        # Five dns lines and the maintenance change. 62 + 8*5 = 102, capped at 90.
        "confidence_min": 90,
        "confidence_max": 90,
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
        # Pool and slow-query records both sit in the window, so neither binds.
        # 40 + (-8) + (-8).
        "root_cause": "cascading observed; cause UNKNOWN",
        "root_cause_keywords": ["unknown"],
        "confidence_min": 24,
        "confidence_max": 24,
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
        "root_cause": "Redis connection failure; redis in notification-service",
        "root_cause_keywords": ["redis", "connection", "notification-service"],
        # Two redis logs and the unreachable event. 62 + 8 + 8.
        "confidence_min": 78,
        "confidence_max": 78,
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
        "root_cause": "flapping observed; cause UNKNOWN",
        "root_cause_keywords": ["unknown"],
        # The pool lines name no dependency, and no cited failure does either.
        # Pool exhaustion does not bind. Cause confidence stays at the
        # symptom-only score, 34.
        "confidence_min": 34,
        "confidence_max": 34,
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
        "root_cause": "data pipeline failure; stale data in recommendation-service",
        "root_cause_keywords": ["pipeline", "stale", "recommendation-service"],
        # The in-window pipeline line and the stale line. Earlier pipeline
        # lines are outside the window. 62 + 8.
        "confidence_min": 70,
        "confidence_max": 70,
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
}
