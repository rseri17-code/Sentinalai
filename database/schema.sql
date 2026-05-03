-- SentinalAI Database Schema
-- PostgreSQL + pgvector for knowledge storage

CREATE EXTENSION IF NOT EXISTS vector;

-- Investigation results
CREATE TABLE IF NOT EXISTS investigations (
    id SERIAL PRIMARY KEY,
    incident_id VARCHAR(50) UNIQUE NOT NULL,
    root_cause TEXT NOT NULL,
    confidence FLOAT NOT NULL,
    reasoning TEXT NOT NULL,
    evidence_timeline JSONB NOT NULL DEFAULT '[]',
    tools_used JSONB NOT NULL DEFAULT '[]',
    investigation_time_seconds FLOAT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Knowledge base for historical incidents
CREATE TABLE IF NOT EXISTS knowledge_base (
    id SERIAL PRIMARY KEY,
    incident_id VARCHAR(50) NOT NULL,
    incident_type VARCHAR(100) NOT NULL,
    root_cause TEXT NOT NULL,
    service VARCHAR(200) NOT NULL,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Tool usage tracking
CREATE TABLE IF NOT EXISTS tool_usage (
    id SERIAL PRIMARY KEY,
    investigation_id INTEGER REFERENCES investigations(id),
    tool_name VARCHAR(200) NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}',
    response JSONB,
    duration_ms INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Ground-truth evaluation results (feeds continuous learning + calibration loop)
CREATE TABLE IF NOT EXISTS eval_results (
    id SERIAL PRIMARY KEY,
    incident_id VARCHAR(50) NOT NULL,
    root_cause_match VARCHAR(20) NOT NULL,
    root_cause_score FLOAT NOT NULL,
    confidence_error FLOAT NOT NULL,
    evidence_coverage FLOAT NOT NULL,
    actual_correct BOOLEAN NOT NULL,
    predicted_confidence INTEGER NOT NULL,
    missing_evidence JSONB NOT NULL DEFAULT '[]',
    evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Knowledge graph nodes (durable, Postgres-backed with TTL eviction)
CREATE TABLE IF NOT EXISTS kg_nodes (
    node_id VARCHAR(200) PRIMARY KEY,
    node_type VARCHAR(50) NOT NULL,
    label TEXT NOT NULL,
    props JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    ttl_expires_at TIMESTAMP WITH TIME ZONE
);

-- Knowledge graph edges
CREATE TABLE IF NOT EXISTS kg_edges (
    edge_id VARCHAR(200) PRIMARY KEY,
    src_id VARCHAR(200) NOT NULL,
    dst_id VARCHAR(200) NOT NULL,
    rel_type VARCHAR(50) NOT NULL,
    weight FLOAT NOT NULL DEFAULT 1.0,
    props JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_investigations_incident_id ON investigations(incident_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_service ON knowledge_base(service);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_type ON knowledge_base(incident_type);
CREATE INDEX IF NOT EXISTS idx_tool_usage_investigation ON tool_usage(investigation_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_incident_id ON eval_results(incident_id);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type ON kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_ttl ON kg_nodes(ttl_expires_at) WHERE ttl_expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges(src_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges(dst_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_rel ON kg_edges(rel_type);

-- -----------------------------------------------------------------------
-- Pattern Intelligence Layer
-- -----------------------------------------------------------------------

-- Telemetry snapshots — rolling 7-day window of golden-signal metrics
CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id SERIAL PRIMARY KEY,
    service VARCHAR(200) NOT NULL,
    source VARCHAR(50) NOT NULL,
    collected_at TIMESTAMP WITH TIME ZONE NOT NULL,
    collected_at_epoch FLOAT NOT NULL,
    metrics JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_service ON telemetry_snapshots(service);
CREATE INDEX IF NOT EXISTS idx_telemetry_epoch   ON telemetry_snapshots(collected_at_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_svc_epoch ON telemetry_snapshots(service, collected_at_epoch DESC);

-- SLO definitions — one row per (service, metric) pair
CREATE TABLE IF NOT EXISTS slo_definitions (
    id SERIAL PRIMARY KEY,
    service VARCHAR(200) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    target FLOAT NOT NULL,
    window_days INTEGER NOT NULL DEFAULT 30,
    threshold_value FLOAT NOT NULL DEFAULT 0.0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_slo_service_metric UNIQUE (service, metric)
);

-- Pattern predictions — every prediction stored for outcome tracking
CREATE TABLE IF NOT EXISTS pattern_predictions (
    id SERIAL PRIMARY KEY,
    prediction_id VARCHAR(36) UNIQUE NOT NULL,
    service VARCHAR(200) NOT NULL,
    pattern_type VARCHAR(50) NOT NULL,
    severity VARCHAR(20) NOT NULL,
    metric VARCHAR(100) NOT NULL,
    confidence FLOAT NOT NULL,
    current_value FLOAT NOT NULL,
    explanation TEXT NOT NULL,
    predicted_breach_hours FLOAT,
    related_service VARCHAR(200) NOT NULL DEFAULT '',
    evidence JSONB NOT NULL DEFAULT '{}',
    published BOOLEAN NOT NULL DEFAULT TRUE,
    outcome VARCHAR(30) NOT NULL DEFAULT 'pending',
    outcome_incident_id VARCHAR(100) NOT NULL DEFAULT '',
    outcome_resolved_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_predictions_service ON pattern_predictions(service);
CREATE INDEX IF NOT EXISTS idx_predictions_outcome ON pattern_predictions(outcome);

-- Investigation outcomes — durable ring buffer for MTTR dashboard persistence
CREATE TABLE IF NOT EXISTS investigation_outcomes (
    id SERIAL PRIMARY KEY,
    investigation_id VARCHAR(100) NOT NULL UNIQUE,
    incident_id VARCHAR(100) NOT NULL,
    incident_type VARCHAR(50) NOT NULL DEFAULT 'unknown',
    service VARCHAR(200) NOT NULL DEFAULT 'unknown',
    root_cause TEXT NOT NULL DEFAULT '',
    confidence FLOAT NOT NULL DEFAULT 0,
    severity INTEGER NOT NULL DEFAULT 3,
    elapsed_ms FLOAT NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    llm_input_tokens INTEGER NOT NULL DEFAULT 0,
    llm_output_tokens INTEGER NOT NULL DEFAULT 0,
    citation_coverage FLOAT NOT NULL DEFAULT 0,
    fix_proposed BOOLEAN NOT NULL DEFAULT FALSE,
    fix_applied BOOLEAN NOT NULL DEFAULT FALSE,
    fix_verified BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_outcomes_recorded_at ON investigation_outcomes(recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_outcomes_service ON investigation_outcomes(service);
CREATE INDEX IF NOT EXISTS idx_predictions_created ON pattern_predictions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_published ON pattern_predictions(published, outcome) WHERE published = TRUE;
