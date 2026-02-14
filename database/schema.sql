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

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_investigations_incident_id ON investigations(incident_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_service ON knowledge_base(service);
CREATE INDEX IF NOT EXISTS idx_knowledge_base_type ON knowledge_base(incident_type);
CREATE INDEX IF NOT EXISTS idx_tool_usage_investigation ON tool_usage(investigation_id);
