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
        "confidence_max": 95,
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
        "confidence_max": 95,
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
}
