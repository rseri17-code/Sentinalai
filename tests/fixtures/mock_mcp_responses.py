"""
Mock MCP responses for test incidents.
Based on actual MCP tool schemas from sentinalai_mcp_tool_catalog.yaml.

Contains 10 test incidents covering:
1. INC12345 - Timeout incident (API Gateway → payment-service)
2. INC12346 - OOMKill incident (user-service memory leak)
3. INC12347 - Error spike after deployment (payment-service NullPointerException)
4. INC12348 - Latency incident (search-service degradation)
5. INC12349 - Resource saturation (order-service CPU exhaustion)
6. INC12350 - Network issue (inter-service connectivity failure)
7. INC12351 - Complex multi-cause incident (cascading failure)
8. INC12352 - Missing data scenario (partial observability)
9. INC12353 - Edge case: flapping alerts (intermittent failures)
10. INC12354 - Edge case: silent failure (no errors, degraded throughput)
"""

# =============================================================================
# TEST INCIDENT 1: INC12345 - Timeout Incident
# =============================================================================
# Incident: "API Gateway timeout spike"
# Expected Root Cause: payment-service database slow queries
# Timeline: DB latency (10:30:10Z) -> Timeouts (10:30:15Z)

INCIDENT_INC12345_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12345",
        "number": "INC0012345",
        "summary": "API Gateway timeout spike",
        "severity": "Critical",
        "status": "In Progress",
        "affected_service": "api-gateway",
        "start_time": "2024-02-12T10:30:15Z",
        "correlated_alerts": 45,
        "timeline": [
            {
                "timestamp": "2024-02-12T10:30:15Z",
                "action": "Incident created",
                "details": "Multiple timeout alerts aggregated",
            }
        ],
    },
    "splunk.search_oneshot_timeout": {
        "results": [
            {
                "_time": "2024-02-12T10:30:15Z",
                "host": "api-gateway-01",
                "level": "ERROR",
                "message": "upstream request timeout: payment-service:8080 (31000ms)",
                "service": "api-gateway",
                "downstream": "payment-service",
            },
            {
                "_time": "2024-02-12T10:30:16Z",
                "host": "api-gateway-02",
                "level": "ERROR",
                "message": "upstream request timeout: payment-service:8080 (30500ms)",
                "service": "api-gateway",
                "downstream": "payment-service",
            },
        ],
        "count": 1247,
        "first_occurrence": "2024-02-12T10:30:15Z",
    },
    "sysdig.golden_signals_payment_service": {
        "golden_signals": {
            "latency": {
                "p50": 28000,
                "p95": 31000,
                "p99": 33000,
                "baseline_p95": 200,
            },
            "traffic": {"rps": 450, "baseline_rps": 500},
            "errors": {"rate": 0.02, "count": 9, "baseline_rate": 0.001},
            "saturation": {"cpu": 45, "memory": 52, "disk": 35},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T10:30:10Z",
        "anomaly_type": "latency_spike",
    },
    "sysdig.query_metrics_payment_service_latency": {
        "intent": "performance",
        "metrics": [
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T10:30:10Z",
                "value": 31000,
            },
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T10:30:11Z",
                "value": 30500,
            },
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T10:30:12Z",
                "value": 31200,
            },
        ],
        "baseline": 200,
        "spike_factor": 155,
    },
    "splunk.get_change_data_payment_service": {
        "changes": [
            {
                "number": "CHG0045678",
                "state": "Scheduled",
                "change_type": "deployment",
                "service": "payment-service",
                "description": "Deploy payment-service v2.3.1",
                "scheduled_start": "2024-02-12T10:29:00Z",
                "status": "successful",
                "risk": "Medium",
            }
        ]
    },
}

# =============================================================================
# TEST INCIDENT 2: INC12346 - OOMKill Incident
# =============================================================================
# Incident: "user-service OOMKilled"
# Expected Root Cause: Memory leak in user-service
# Timeline: Memory gradual increase -> OOMKill

INCIDENT_INC12346_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12346",
        "number": "INC0012346",
        "summary": "user-service OOMKilled",
        "severity": "High",
        "status": "In Progress",
        "affected_service": "user-service",
        "start_time": "2024-02-12T14:22:33Z",
    },
    "splunk.search_oneshot_oomkill": {
        "results": [
            {
                "_time": "2024-02-12T14:22:33Z",
                "level": "ERROR",
                "message": "OOMKilled: user-service-7d8f9c",
                "service": "user-service",
                "container": "user-service-7d8f9c",
                "namespace": "production",
            },
            {
                "_time": "2024-02-12T14:22:33Z",
                "level": "INFO",
                "message": "Restarting container user-service-7d8f9c",
                "service": "user-service",
            },
        ],
        "count": 3,
        "first_occurrence": "2024-02-12T14:22:33Z",
    },
    "sysdig.query_metrics_user_service_memory": {
        "intent": "performance",
        "metrics": [
            {
                "timestamp": "2024-02-12T13:00:00Z",
                "name": "memory_usage_bytes",
                "value": 6500000000,
            },
            {
                "timestamp": "2024-02-12T13:30:00Z",
                "name": "memory_usage_bytes",
                "value": 7000000000,
            },
            {
                "timestamp": "2024-02-12T14:00:00Z",
                "name": "memory_usage_bytes",
                "value": 7500000000,
            },
            {
                "timestamp": "2024-02-12T14:20:00Z",
                "name": "memory_usage_bytes",
                "value": 7900000000,
            },
            {
                "timestamp": "2024-02-12T14:22:30Z",
                "name": "memory_usage_bytes",
                "value": 8100000000,
            },
        ],
        "limit": 8000000000,
        "pattern": "gradual_increase",
    },
    "sysdig.get_events_user_service": {
        "events": [
            {
                "type": "pod_restart",
                "severity": "high",
                "message": "Pod user-service-7d8f9c restarted due to OOMKilled",
                "timestamp": "2024-02-12T14:22:33Z",
            }
        ]
    },
    "splunk.search_oneshot_memory_logs": {
        "results": [
            {
                "_time": "2024-02-12T14:22:00Z",
                "level": "WARN",
                "message": "High heap usage: 7.8GB / 8GB",
                "service": "user-service",
            }
        ],
        "count": 45,
    },
}

# =============================================================================
# TEST INCIDENT 3: INC12347 - Error Spike After Deployment
# =============================================================================
# Incident: "Payment service error spike"
# Expected Root Cause: Deployment introduced null pointer exception
# Timeline: Deployment (09:00:00Z) -> Errors (09:00:05Z)

INCIDENT_INC12347_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12347",
        "number": "INC0012347",
        "summary": "Payment service error spike",
        "severity": "Critical",
        "status": "In Progress",
        "affected_service": "payment-service",
        "start_time": "2024-02-12T09:00:05Z",
    },
    "splunk.search_oneshot_errors": {
        "results": [
            {
                "_time": "2024-02-12T09:00:05Z",
                "level": "ERROR",
                "message": "NullPointerException in PaymentProcessor.processRefund()",
                "service": "payment-service",
                "exception": "java.lang.NullPointerException",
                "stack_trace": "at PaymentProcessor.processRefund:142",
            },
        ],
        "count": 524,
        "first_occurrence": "2024-02-12T09:00:05Z",
        "error_types": {"NullPointerException": 524},
    },
    "splunk.app_change_data_payment_service": {
        "app_number": "APP123",
        "changes": [
            {
                "change_id": "CHG0098765",
                "number": "CHG0098765",
                "change_type": "deployment",
                "service": "payment-service",
                "description": "Deploy payment-service v3.1.0 - Add refund feature",
                "scheduled_start": "2024-02-12T09:00:00Z",
                "actual_start": "2024-02-12T09:00:00Z",
                "status": "successful",
                "risk": "Medium",
            }
        ],
    },
    "sysdig.get_events_payment_service": {
        "events": [
            {
                "type": "deployment",
                "severity": "info",
                "message": "Deployment payment-service v3.1.0 completed",
                "timestamp": "2024-02-12T09:00:00Z",
            }
        ]
    },
    "sysdig.golden_signals_payment_service": {
        "golden_signals": {
            "latency": {
                "p50": 250,
                "p95": 450,
                "p99": 600,
                "baseline_p95": 200,
            },
            "traffic": {"rps": 500},
            "errors": {"rate": 0.35, "count": 524, "baseline_rate": 0.001},
            "saturation": {"cpu": 55, "memory": 60, "disk": 40},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T09:00:05Z",
        "anomaly_type": "error_spike",
    },
}

# =============================================================================
# TEST INCIDENT 4: INC12348 - Latency Incident
# =============================================================================
# Incident: "search-service response time degradation"
# Expected Root Cause: Elasticsearch cluster rebalancing causing slow queries
# Timeline: ES rebalance (11:00:00Z) -> Latency (11:00:05Z)

INCIDENT_INC12348_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12348",
        "number": "INC0012348",
        "summary": "search-service response time degradation",
        "severity": "High",
        "status": "In Progress",
        "affected_service": "search-service",
        "start_time": "2024-02-12T11:00:05Z",
        "correlated_alerts": 12,
    },
    "splunk.search_oneshot_latency": {
        "results": [
            {
                "_time": "2024-02-12T11:00:05Z",
                "host": "search-service-01",
                "level": "WARN",
                "message": "Slow query: elasticsearch response took 8500ms (threshold: 500ms)",
                "service": "search-service",
                "backend": "elasticsearch",
            },
            {
                "_time": "2024-02-12T11:00:06Z",
                "host": "search-service-02",
                "level": "WARN",
                "message": "Slow query: elasticsearch response took 9200ms (threshold: 500ms)",
                "service": "search-service",
                "backend": "elasticsearch",
            },
        ],
        "count": 342,
        "first_occurrence": "2024-02-12T11:00:05Z",
    },
    "sysdig.golden_signals_search_service": {
        "golden_signals": {
            "latency": {
                "p50": 5200,
                "p95": 8500,
                "p99": 12000,
                "baseline_p95": 350,
            },
            "traffic": {"rps": 800, "baseline_rps": 820},
            "errors": {"rate": 0.05, "count": 40, "baseline_rate": 0.002},
            "saturation": {"cpu": 30, "memory": 45, "disk": 70},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T11:00:00Z",
        "anomaly_type": "latency_spike",
    },
    "sysdig.query_metrics_search_service_latency": {
        "intent": "performance",
        "metrics": [
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T10:59:55Z",
                "value": 350,
            },
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T11:00:00Z",
                "value": 4500,
            },
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T11:00:05Z",
                "value": 8500,
            },
            {
                "name": "response_time_ms",
                "timestamp": "2024-02-12T11:00:10Z",
                "value": 9200,
            },
        ],
        "baseline": 350,
        "spike_factor": 24,
    },
    "splunk.search_oneshot_elasticsearch": {
        "results": [
            {
                "_time": "2024-02-12T11:00:00Z",
                "level": "WARN",
                "message": "Cluster rebalancing started: shard relocation in progress",
                "service": "elasticsearch",
                "event_type": "cluster_rebalance",
            },
            {
                "_time": "2024-02-12T11:00:01Z",
                "level": "INFO",
                "message": "Moving shard [products][2] from node-3 to node-5",
                "service": "elasticsearch",
            },
        ],
        "count": 15,
        "first_occurrence": "2024-02-12T11:00:00Z",
    },
    "splunk.get_change_data_search_service": {
        "changes": [],
    },
}

# =============================================================================
# TEST INCIDENT 5: INC12349 - Resource Saturation
# =============================================================================
# Incident: "order-service CPU exhaustion"
# Expected Root Cause: Infinite loop in order validation after config change
# Timeline: Config change (15:00:00Z) -> CPU spike (15:00:02Z)

INCIDENT_INC12349_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12349",
        "number": "INC0012349",
        "summary": "order-service CPU exhaustion",
        "severity": "Critical",
        "status": "In Progress",
        "affected_service": "order-service",
        "start_time": "2024-02-12T15:00:02Z",
        "correlated_alerts": 8,
    },
    "sysdig.golden_signals_order_service": {
        "golden_signals": {
            "latency": {
                "p50": 15000,
                "p95": 28000,
                "p99": 30000,
                "baseline_p95": 180,
            },
            "traffic": {"rps": 120, "baseline_rps": 600},
            "errors": {"rate": 0.45, "count": 270, "baseline_rate": 0.003},
            "saturation": {"cpu": 99, "memory": 78, "disk": 40},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T15:00:02Z",
        "anomaly_type": "saturation",
    },
    "sysdig.query_metrics_order_service_cpu": {
        "intent": "resource",
        "metrics": [
            {
                "name": "cpu_usage_percent",
                "timestamp": "2024-02-12T14:59:58Z",
                "value": 35,
            },
            {
                "name": "cpu_usage_percent",
                "timestamp": "2024-02-12T15:00:02Z",
                "value": 98,
            },
            {
                "name": "cpu_usage_percent",
                "timestamp": "2024-02-12T15:00:05Z",
                "value": 99,
            },
            {
                "name": "cpu_usage_percent",
                "timestamp": "2024-02-12T15:00:10Z",
                "value": 99,
            },
        ],
        "baseline": 35,
        "spike_factor": 2.8,
    },
    "splunk.search_oneshot_cpu": {
        "results": [
            {
                "_time": "2024-02-12T15:00:02Z",
                "host": "order-service-01",
                "level": "ERROR",
                "message": "Thread pool exhaustion: all worker threads busy in order-validation loop",
                "service": "order-service",
            },
            {
                "_time": "2024-02-12T15:00:03Z",
                "host": "order-service-02",
                "level": "ERROR",
                "message": "Thread pool exhaustion: all worker threads busy in order-validation loop",
                "service": "order-service",
            },
        ],
        "count": 890,
        "first_occurrence": "2024-02-12T15:00:02Z",
    },
    "splunk.get_change_data_order_service": {
        "changes": [
            {
                "number": "CHG0056789",
                "change_type": "config_change",
                "service": "order-service",
                "description": "Update order validation rules configuration",
                "scheduled_start": "2024-02-12T15:00:00Z",
                "status": "successful",
                "risk": "Low",
            }
        ]
    },
}

# =============================================================================
# TEST INCIDENT 6: INC12350 - Network Issue
# =============================================================================
# Incident: "Inter-service connectivity failure"
# Expected Root Cause: DNS resolution failure after DNS server maintenance
# Timeline: DNS maintenance (16:30:00Z) -> Connection failures (16:30:10Z)

INCIDENT_INC12350_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12350",
        "number": "INC0012350",
        "summary": "Inter-service connectivity failure",
        "severity": "Critical",
        "status": "In Progress",
        "affected_service": "inventory-service",
        "start_time": "2024-02-12T16:30:10Z",
        "correlated_alerts": 28,
    },
    "splunk.search_oneshot_network": {
        "results": [
            {
                "_time": "2024-02-12T16:30:10Z",
                "host": "inventory-service-01",
                "level": "ERROR",
                "message": "Connection refused: unable to resolve hostname catalog-service.internal",
                "service": "inventory-service",
                "error_type": "dns_resolution_failure",
            },
            {
                "_time": "2024-02-12T16:30:10Z",
                "host": "checkout-service-01",
                "level": "ERROR",
                "message": "Connection refused: unable to resolve hostname payment-service.internal",
                "service": "checkout-service",
                "error_type": "dns_resolution_failure",
            },
            {
                "_time": "2024-02-12T16:30:11Z",
                "host": "api-gateway-01",
                "level": "ERROR",
                "message": "Connection refused: unable to resolve hostname auth-service.internal",
                "service": "api-gateway",
                "error_type": "dns_resolution_failure",
            },
        ],
        "count": 3456,
        "first_occurrence": "2024-02-12T16:30:10Z",
    },
    "sysdig.golden_signals_inventory_service": {
        "golden_signals": {
            "latency": {
                "p50": 30000,
                "p95": 30000,
                "p99": 30000,
                "baseline_p95": 120,
            },
            "traffic": {"rps": 10, "baseline_rps": 400},
            "errors": {"rate": 0.95, "count": 3456, "baseline_rate": 0.001},
            "saturation": {"cpu": 15, "memory": 30, "disk": 25},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T16:30:10Z",
        "anomaly_type": "error_spike",
    },
    "splunk.search_oneshot_dns": {
        "results": [
            {
                "_time": "2024-02-12T16:30:00Z",
                "level": "INFO",
                "message": "DNS server kube-dns entering maintenance mode",
                "service": "kube-dns",
                "event_type": "maintenance",
            },
            {
                "_time": "2024-02-12T16:30:05Z",
                "level": "WARN",
                "message": "DNS cache flush completed, rebuilding entries",
                "service": "kube-dns",
            },
        ],
        "count": 8,
        "first_occurrence": "2024-02-12T16:30:00Z",
    },
    "splunk.get_change_data_infrastructure": {
        "changes": [
            {
                "number": "CHG0067890",
                "change_type": "maintenance",
                "service": "kube-dns",
                "description": "DNS server scheduled maintenance and cache flush",
                "scheduled_start": "2024-02-12T16:30:00Z",
                "status": "successful",
                "risk": "Low",
            }
        ]
    },
}

# =============================================================================
# TEST INCIDENT 7: INC12351 - Complex Multi-Cause Incident
# =============================================================================
# Incident: "Cascading failure across checkout flow"
# Expected Root Cause: Database connection pool exhaustion in payment-service
#   caused by slow queries from a bad index drop, cascading to checkout and api-gateway
# Timeline: Index dropped (12:00:00Z) -> DB slow (12:00:05Z) -> Pool exhaustion (12:01:00Z)
#   -> Checkout failures (12:01:05Z) -> Gateway timeouts (12:01:10Z)

INCIDENT_INC12351_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12351",
        "number": "INC0012351",
        "summary": "Cascading failure across checkout flow",
        "severity": "Critical",
        "status": "In Progress",
        "affected_service": "api-gateway",
        "start_time": "2024-02-12T12:01:10Z",
        "correlated_alerts": 67,
    },
    "splunk.search_oneshot_errors_cascade": {
        "results": [
            {
                "_time": "2024-02-12T12:00:05Z",
                "host": "payment-db-01",
                "level": "WARN",
                "message": "Slow query detected: SELECT * FROM transactions WHERE order_id=... (12000ms, full table scan)",
                "service": "payment-db",
            },
            {
                "_time": "2024-02-12T12:01:00Z",
                "host": "payment-service-01",
                "level": "ERROR",
                "message": "Connection pool exhausted: 50/50 connections in use, 120 requests waiting",
                "service": "payment-service",
                "error_type": "connection_pool_exhaustion",
            },
            {
                "_time": "2024-02-12T12:01:05Z",
                "host": "checkout-service-01",
                "level": "ERROR",
                "message": "Payment processing failed: upstream payment-service timeout",
                "service": "checkout-service",
            },
            {
                "_time": "2024-02-12T12:01:10Z",
                "host": "api-gateway-01",
                "level": "ERROR",
                "message": "upstream request timeout: checkout-service:8080",
                "service": "api-gateway",
            },
        ],
        "count": 2340,
        "first_occurrence": "2024-02-12T12:00:05Z",
    },
    "sysdig.golden_signals_payment_service": {
        "golden_signals": {
            "latency": {
                "p50": 12000,
                "p95": 30000,
                "p99": 30000,
                "baseline_p95": 150,
            },
            "traffic": {"rps": 50, "baseline_rps": 400},
            "errors": {"rate": 0.88, "count": 2100, "baseline_rate": 0.002},
            "saturation": {"cpu": 90, "memory": 85, "disk": 45},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T12:00:05Z",
        "anomaly_type": "latency_spike",
    },
    "sysdig.golden_signals_checkout_service": {
        "golden_signals": {
            "latency": {
                "p50": 30000,
                "p95": 30000,
                "p99": 30000,
                "baseline_p95": 300,
            },
            "traffic": {"rps": 20, "baseline_rps": 350},
            "errors": {"rate": 0.92, "count": 1800, "baseline_rate": 0.001},
            "saturation": {"cpu": 25, "memory": 40, "disk": 30},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T12:01:05Z",
        "anomaly_type": "error_spike",
    },
    "splunk.get_change_data_payment_db": {
        "changes": [
            {
                "number": "CHG0078901",
                "change_type": "database_migration",
                "service": "payment-db",
                "description": "Drop unused index idx_transactions_legacy on transactions table",
                "scheduled_start": "2024-02-12T12:00:00Z",
                "status": "successful",
                "risk": "Low",
            }
        ]
    },
    "sysdig.query_metrics_payment_service_connections": {
        "intent": "resource",
        "metrics": [
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T12:00:00Z",
                "value": 10,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T12:00:30Z",
                "value": 35,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T12:01:00Z",
                "value": 50,
            },
        ],
        "pool_max": 50,
    },
}

# =============================================================================
# TEST INCIDENT 8: INC12352 - Missing Data Scenario
# =============================================================================
# Incident: "notification-service degraded"
# Expected Root Cause: Must handle gracefully with partial data - Redis connection issues
# Timeline: Some tools return empty/error, must still produce reasonable RCA

INCIDENT_INC12352_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12352",
        "number": "INC0012352",
        "summary": "notification-service degraded",
        "severity": "Medium",
        "status": "In Progress",
        "affected_service": "notification-service",
        "start_time": "2024-02-12T17:15:00Z",
        "correlated_alerts": 5,
    },
    "splunk.search_oneshot_notification": {
        "results": [
            {
                "_time": "2024-02-12T17:15:00Z",
                "host": "notification-service-01",
                "level": "ERROR",
                "message": "Redis connection refused: ECONNREFUSED 10.0.5.12:6379",
                "service": "notification-service",
                "error_type": "connection_refused",
            },
            {
                "_time": "2024-02-12T17:15:01Z",
                "host": "notification-service-02",
                "level": "ERROR",
                "message": "Failed to enqueue notification: Redis unavailable",
                "service": "notification-service",
            },
        ],
        "count": 156,
        "first_occurrence": "2024-02-12T17:15:00Z",
    },
    # Sysdig metrics return EMPTY - simulating missing data
    "sysdig.golden_signals_notification_service": {
        "error": "metrics_unavailable",
        "message": "No metrics data available for notification-service in the requested time window",
    },
    # Some data still available
    "sysdig.get_events_notification_service": {
        "events": [
            {
                "type": "connection_failure",
                "severity": "high",
                "message": "Redis cluster node-1 unreachable",
                "timestamp": "2024-02-12T17:14:55Z",
            }
        ]
    },
    "splunk.get_change_data_notification_service": {
        "changes": [],
    },
}

# =============================================================================
# TEST INCIDENT 9: INC12353 - Edge Case: Flapping Alerts
# =============================================================================
# Incident: "auth-service intermittent failures"
# Expected Root Cause: Connection pool leak causing intermittent exhaustion
# Timeline: Sporadic failures every 5-10 minutes

INCIDENT_INC12353_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12353",
        "number": "INC0012353",
        "summary": "auth-service intermittent failures",
        "severity": "Medium",
        "status": "In Progress",
        "affected_service": "auth-service",
        "start_time": "2024-02-12T08:00:00Z",
        "correlated_alerts": 35,
        "alert_pattern": "flapping",
    },
    "splunk.search_oneshot_auth_errors": {
        "results": [
            {
                "_time": "2024-02-12T08:00:00Z",
                "level": "ERROR",
                "message": "Authentication failed: connection pool exhausted",
                "service": "auth-service",
            },
            {
                "_time": "2024-02-12T08:05:30Z",
                "level": "ERROR",
                "message": "Authentication failed: connection pool exhausted",
                "service": "auth-service",
            },
            {
                "_time": "2024-02-12T08:12:00Z",
                "level": "ERROR",
                "message": "Authentication failed: connection pool exhausted",
                "service": "auth-service",
            },
            {
                "_time": "2024-02-12T08:20:15Z",
                "level": "ERROR",
                "message": "Authentication failed: connection pool exhausted",
                "service": "auth-service",
            },
        ],
        "count": 89,
        "first_occurrence": "2024-02-12T08:00:00Z",
        "pattern": "intermittent",
    },
    "sysdig.golden_signals_auth_service": {
        "golden_signals": {
            "latency": {
                "p50": 50,
                "p95": 200,
                "p99": 30000,
                "baseline_p95": 80,
            },
            "traffic": {"rps": 1200, "baseline_rps": 1200},
            "errors": {"rate": 0.03, "count": 89, "baseline_rate": 0.001},
            "saturation": {"cpu": 25, "memory": 60, "disk": 20},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T08:00:00Z",
        "anomaly_type": "intermittent_errors",
    },
    "sysdig.query_metrics_auth_service_connections": {
        "intent": "resource",
        "metrics": [
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T08:00:00Z",
                "value": 50,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T08:02:00Z",
                "value": 30,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T08:05:30Z",
                "value": 50,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T08:07:00Z",
                "value": 28,
            },
            {
                "name": "db_connection_pool_active",
                "timestamp": "2024-02-12T08:12:00Z",
                "value": 50,
            },
        ],
        "pool_max": 50,
        "pattern": "sawtooth",
    },
    "splunk.get_change_data_auth_service": {
        "changes": [],
    },
}

# =============================================================================
# TEST INCIDENT 10: INC12354 - Edge Case: Silent Failure
# =============================================================================
# Incident: "recommendation-service throughput drop"
# Expected Root Cause: Upstream data pipeline stalled, no errors but no fresh data
# Timeline: Pipeline stall (13:00:00Z) -> Stale cache (13:30:00Z) -> Throughput drop (13:45:00Z)

INCIDENT_INC12354_MOCKS = {
    "moogsoft.get_incident_by_id": {
        "incident_id": "INC12354",
        "number": "INC0012354",
        "summary": "recommendation-service throughput drop",
        "severity": "Medium",
        "status": "In Progress",
        "affected_service": "recommendation-service",
        "start_time": "2024-02-12T13:45:00Z",
        "correlated_alerts": 3,
    },
    "splunk.search_oneshot_recommendation": {
        "results": [
            {
                "_time": "2024-02-12T13:30:00Z",
                "level": "WARN",
                "message": "Cache miss rate elevated: 85% (baseline: 15%)",
                "service": "recommendation-service",
            },
            {
                "_time": "2024-02-12T13:45:00Z",
                "level": "WARN",
                "message": "Serving stale recommendations: data freshness > 45 minutes",
                "service": "recommendation-service",
            },
        ],
        "count": 12,
        "first_occurrence": "2024-02-12T13:30:00Z",
    },
    "sysdig.golden_signals_recommendation_service": {
        "golden_signals": {
            "latency": {
                "p50": 120,
                "p95": 200,
                "p99": 350,
                "baseline_p95": 180,
            },
            "traffic": {"rps": 150, "baseline_rps": 500},
            "errors": {"rate": 0.005, "count": 3, "baseline_rate": 0.002},
            "saturation": {"cpu": 10, "memory": 25, "disk": 30},
        },
        "anomaly_detected": True,
        "anomaly_start": "2024-02-12T13:45:00Z",
        "anomaly_type": "throughput_drop",
    },
    "splunk.search_oneshot_pipeline": {
        "results": [
            {
                "_time": "2024-02-12T13:00:00Z",
                "level": "ERROR",
                "message": "Data pipeline job recommendation-etl failed: connection to data warehouse timed out",
                "service": "data-pipeline",
                "job_name": "recommendation-etl",
            },
            {
                "_time": "2024-02-12T13:15:00Z",
                "level": "ERROR",
                "message": "Data pipeline job recommendation-etl retry 1/3 failed",
                "service": "data-pipeline",
            },
            {
                "_time": "2024-02-12T13:30:00Z",
                "level": "ERROR",
                "message": "Data pipeline job recommendation-etl retry 3/3 failed, giving up",
                "service": "data-pipeline",
            },
        ],
        "count": 5,
        "first_occurrence": "2024-02-12T13:00:00Z",
    },
    "sysdig.query_metrics_recommendation_service_traffic": {
        "intent": "traffic",
        "metrics": [
            {
                "name": "requests_per_second",
                "timestamp": "2024-02-12T13:00:00Z",
                "value": 500,
            },
            {
                "name": "requests_per_second",
                "timestamp": "2024-02-12T13:30:00Z",
                "value": 350,
            },
            {
                "name": "requests_per_second",
                "timestamp": "2024-02-12T13:45:00Z",
                "value": 150,
            },
        ],
        "baseline": 500,
    },
    "splunk.get_change_data_recommendation_service": {
        "changes": [],
    },
}

# =============================================================================
# COMBINED MOCK RESPONSES (for testing framework)
# =============================================================================

ALL_MOCKS = {
    "INC12345": INCIDENT_INC12345_MOCKS,
    "INC12346": INCIDENT_INC12346_MOCKS,
    "INC12347": INCIDENT_INC12347_MOCKS,
    "INC12348": INCIDENT_INC12348_MOCKS,
    "INC12349": INCIDENT_INC12349_MOCKS,
    "INC12350": INCIDENT_INC12350_MOCKS,
    "INC12351": INCIDENT_INC12351_MOCKS,
    "INC12352": INCIDENT_INC12352_MOCKS,
    "INC12353": INCIDENT_INC12353_MOCKS,
    "INC12354": INCIDENT_INC12354_MOCKS,
}
