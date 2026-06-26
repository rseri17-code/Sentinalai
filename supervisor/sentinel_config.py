"""Centralised configuration for SentinalAI.

All env-var reads are concentrated here so that:
- Every knob is visible in one place.
- Tests can override and tear down cleanly via reset_config().
- Startup validation catches bad config before the first request.

SECRETS (API tokens, webhook secrets, passwords) are intentionally absent.
They are short-lived, must never be logged, and are read at call-time directly
from os.environ where they are used. Only non-sensitive config lives here.

Usage::

    from supervisor.sentinel_config import get_config
    cfg = get_config()
    if cfg.supervisor.yaml_playbooks_enabled:
        ...

Tests::

    from supervisor.sentinel_config import reset_config
    monkeypatch.setenv("LLM_ENABLED", "true")
    reset_config()
    cfg = get_config()
    assert cfg.supervisor.llm_enabled
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bool(key: str, default: str = "false") -> bool:
    return os.environ.get(key, default).lower() in ("1", "true", "yes")

def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Config %s=%r is not an integer; using default %d", key, raw, default)
        return default

def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Config %s=%r is not a float; using default %f", key, raw, default)
        return default

def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# Sub-sections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SupervisorConfig:
    # Feature flags
    llm_enabled: bool
    agentic_planner: bool
    yaml_playbooks_enabled: bool
    loop_controller_enabled: bool
    strategy_evolver_enabled: bool
    alert_dedup_enabled: bool
    gap_aggregator_enabled: bool
    agui_enabled: bool

    # Loop controller
    loop_convergence_threshold: float
    loop_max_nudges: int

    # Budget / guardrails
    investigation_budget_max_calls: int
    mcp_call_timeout_seconds: int
    loop_checkpoint_interval: int
    loop_max_stall_checkpoints: int

    # Planner
    planner_max_iterations: int
    planner_stagnation_detection: bool

    # Strategy evolver
    ema_alpha: float
    min_calls_to_evolve: int
    evolved_strategy_path: str

    # Alert dedup
    dedup_db_path: str
    dedup_cooldown_critical_secs: int
    dedup_cooldown_medium_secs: int
    dedup_cooldown_low_secs: int
    dedup_correlation_window_secs: int

    # Gap aggregator
    gap_aggregator_path: str
    gap_persistent_threshold: float

    # Playbooks
    playbooks_dir: str

    # Pattern registry
    pattern_registry_path: str


@dataclass(frozen=True)
class IntelligenceConfig:
    # Paths
    investigations_dir: str
    resolution_outcomes_path: str
    service_profiles_path: str
    pattern_signatures_path: str

    # Semantic search
    semantic_backend: str

    # Telemetry
    telemetry_enabled: bool
    telemetry_poll_interval_sec: int
    telemetry_retention_days: int
    telemetry_collect_timeout_sec: int

    # Background runner
    intelligence_enabled: bool
    intelligence_prune_interval_sec: int
    intelligence_severity_gate: str

    # Pattern detection
    pattern_match_threshold: float
    pattern_min_frequency: int
    pattern_signatures_max: int
    pattern_detector_enabled: bool

    # Service profiles
    service_profile_max_history: int

    # SLO
    slo_enabled: bool
    slo_window_days: int

    # ITSM writeback (security: must default false — see CLAUDE.md)
    itsm_writeback_enabled: bool

    # Wiki promoter
    wiki_promote_threshold: int

    # Prediction store
    prediction_cooldown_minutes: int
    prediction_retention_days: int

    # Citation anti-hallucination
    citation_anti_hallucination_enabled: bool
    citation_anti_hallucination_floor: float

    # Monitored services (comma-separated)
    monitored_services: str


@dataclass(frozen=True)
class AguiConfig:
    # Server
    bff_host: str
    bff_port: int
    public_url: str
    dev_reload: bool

    # Auth
    auth_required: bool
    audience: str
    cognito_jwks_url: str

    # Security middleware
    honeypot: bool
    session_ttl: int

    # ITSM writeback gate (duplicate of intelligence; checked at API layer)
    itsm_writeback_enabled: bool

    # Webhook auth
    require_webhook_auth: bool

    # S3 / DynamoDB
    s3_bucket: str
    dynamodb_table: str
    local_receipt_dir: str

    # CORS
    allowed_origins: str


@dataclass(frozen=True)
class DatabaseConfig:
    database_url: str
    pool_size: int
    pool_overflow: int
    pool_timeout: int
    pool_recycle: int
    echo: bool

    # OPS persistence
    ops_db_path: str
    ops_db_enabled: bool
    ops_retention_receipts_days: int
    ops_retention_history_days: int
    ops_retention_safety_days: int
    ops_queue_max: int
    ops_batch_size: int
    ops_batch_delay: float


@dataclass(frozen=True)
class WorkersConfig:
    # AWS / AgentCore gateway
    aws_region: str
    agentcore_gateway_url: str
    gateway_token_refresh_buffer_seconds: int

    # MCP client
    mcp_call_timeout_seconds: int
    mcp_max_retries: int
    mcp_dedup_enabled: bool

    # AgentCore target names
    agentcore_target_moogsoft: str
    agentcore_target_splunk: str
    agentcore_target_sysdig: str
    agentcore_target_signalfx: str
    agentcore_target_dynatrace: str
    agentcore_target_servicenow: str
    agentcore_target_github: str
    agentcore_target_confluence: str
    agentcore_target_kubernetes: str

    # Tool discovery
    tool_discovery_ttl_seconds: int

    # ThousandEyes (token excluded — security constraint)
    enable_thousandeyes_rca: bool
    te_use_fixtures: bool
    te_mcp_url: str
    te_timeout: int

    # Visual evidence
    visual_evidence_enabled: bool
    visual_evidence_fetch_images: bool

    # Git worker
    git_worker_enabled: bool
    git_bisect_max_commits: int

    # Code worker
    code_worker_min_confidence: int
    code_worker_max_diff_chars: int


@dataclass(frozen=True)
class IntegrationsConfig:
    # Slack (webhooks/tokens excluded)
    slack_rca_channel: str
    slack_intel_channel: str
    notify_timeout_sec: int
    notify_min_confidence: float

    # OpsGenie (key excluded)
    opsgenie_api_url: str

    # Tenant config
    tenant_config_path: str
    default_org_id: str


@dataclass(frozen=True)
class SentinelConfig:
    supervisor: SupervisorConfig
    intelligence: IntelligenceConfig
    agui: AguiConfig
    database: DatabaseConfig
    workers: WorkersConfig
    integrations: IntegrationsConfig
    environment: str
    log_level: str

    @classmethod
    def from_env(cls) -> "SentinelConfig":
        """Build SentinelConfig from the current process environment."""
        _repo_root = str(Path(__file__).parent.parent)

        supervisor = SupervisorConfig(
            llm_enabled=_bool("LLM_ENABLED", "false"),
            agentic_planner=_bool("AGENTIC_PLANNER", "false"),
            yaml_playbooks_enabled=_bool("YAML_PLAYBOOKS_ENABLED", "false"),
            loop_controller_enabled=_bool("LOOP_CONTROLLER_ENABLED", "false"),
            strategy_evolver_enabled=_bool("STRATEGY_EVOLVER_ENABLED", "false"),
            alert_dedup_enabled=_bool("ALERT_DEDUP_ENABLED", "true"),
            gap_aggregator_enabled=_bool("GAP_AGGREGATOR_ENABLED", "false"),
            agui_enabled=_bool("AGUI_ENABLED", "true"),
            loop_convergence_threshold=_float("LOOP_CONVERGENCE_THRESHOLD", 0.72),
            loop_max_nudges=_int("LOOP_MAX_NUDGES", 2),
            investigation_budget_max_calls=_int("INVESTIGATION_BUDGET_MAX_CALLS", 20),
            mcp_call_timeout_seconds=_int("MCP_CALL_TIMEOUT_SECONDS", 30),
            loop_checkpoint_interval=_int("LOOP_CHECKPOINT_INTERVAL", 4),
            loop_max_stall_checkpoints=_int("LOOP_MAX_STALL_CHECKPOINTS", 2),
            planner_max_iterations=_int("PLANNER_MAX_ITERATIONS", 10),
            planner_stagnation_detection=_bool("PLANNER_STAGNATION_DETECTION", "false"),
            ema_alpha=_float("EMA_ALPHA", 0.12),
            min_calls_to_evolve=_int("MIN_CALLS_TO_EVOLVE", 5),
            evolved_strategy_path=_str("EVOLVED_STRATEGY_PATH", "eval/evolved_strategy.json"),
            dedup_db_path=_str("DEDUP_DB_PATH", "eval/dedup.db"),
            dedup_cooldown_critical_secs=_int("DEDUP_COOLDOWN_CRITICAL_SECS", 300),
            dedup_cooldown_medium_secs=_int("DEDUP_COOLDOWN_MEDIUM_SECS", 900),
            dedup_cooldown_low_secs=_int("DEDUP_COOLDOWN_LOW_SECS", 1800),
            dedup_correlation_window_secs=_int("DEDUP_CORRELATION_WINDOW_SECS", 600),
            gap_aggregator_path=_str("GAP_AGGREGATOR_PATH", "eval/gap_aggregator.json"),
            gap_persistent_threshold=_float("GAP_PERSISTENT_THRESHOLD", 0.50),
            playbooks_dir=_str("PLAYBOOKS_DIR", os.path.join(_repo_root, "config", "playbooks")),
            pattern_registry_path=_str("PATTERN_REGISTRY_PATH", "eval/pattern_registry.json"),
        )

        intelligence = IntelligenceConfig(
            investigations_dir=_str("INVESTIGATIONS_DIR", "eval/investigations"),
            resolution_outcomes_path=_str("RESOLUTION_OUTCOMES_PATH", "eval/resolution_outcomes.jsonl"),
            service_profiles_path=_str("SERVICE_PROFILES_PATH", "eval/service_profiles.json"),
            pattern_signatures_path=_str("PATTERN_SIGNATURES_PATH", "eval/pattern_signatures.json"),
            semantic_backend=_str("SEMANTIC_BACKEND", "tfidf"),
            telemetry_enabled=_bool("TELEMETRY_ENABLED", "true"),
            telemetry_poll_interval_sec=_int("TELEMETRY_POLL_INTERVAL_SEC", 60),
            telemetry_retention_days=_int("TELEMETRY_RETENTION_DAYS", 7),
            telemetry_collect_timeout_sec=_int("TELEMETRY_COLLECT_TIMEOUT_SEC", 10),
            intelligence_enabled=_bool("INTELLIGENCE_ENABLED", "true"),
            intelligence_prune_interval_sec=_int("INTELLIGENCE_PRUNE_INTERVAL_SEC", 3600),
            intelligence_severity_gate=_str("INTELLIGENCE_SEVERITY_GATE", "WATCH"),
            pattern_match_threshold=_float("PATTERN_MATCH_THRESHOLD", 0.72),
            pattern_min_frequency=_int("PATTERN_MIN_FREQUENCY", 2),
            pattern_signatures_max=_int("PATTERN_SIGNATURES_MAX", 1000),
            pattern_detector_enabled=_bool("PATTERN_DETECTOR_ENABLED", "true"),
            service_profile_max_history=_int("SERVICE_PROFILE_MAX_HISTORY", 200),
            slo_enabled=_bool("SLO_ENABLED", "true"),
            slo_window_days=_int("SLO_WINDOW_DAYS", 30),
            itsm_writeback_enabled=_bool("ITSM_WRITEBACK_ENABLED", "false"),
            wiki_promote_threshold=_int("WIKI_PROMOTE_THRESHOLD", 3),
            prediction_cooldown_minutes=_int("PREDICTION_COOLDOWN_MINUTES", 30),
            prediction_retention_days=_int("PREDICTION_RETENTION_DAYS", 30),
            citation_anti_hallucination_enabled=_bool("CITATION_ANTI_HALLUCINATION_ENABLED", "true"),
            citation_anti_hallucination_floor=_float("CITATION_ANTI_HALLUCINATION_FLOOR", 0.70),
            monitored_services=_str("MONITORED_SERVICES", ""),
        )

        agui = AguiConfig(
            bff_host=_str("AGUI_BFF_HOST", "0.0.0.0"),
            bff_port=_int("AGUI_BFF_PORT", 8081),
            public_url=_str("AGUI_PUBLIC_URL", "http://localhost:8081"),
            dev_reload=_bool("AGUI_DEV_RELOAD", "false"),
            auth_required=_bool("AGUI_AUTH_REQUIRED", "true"),
            audience=_str("AGUI_AUDIENCE", "agui"),
            cognito_jwks_url=_str("AGUI_COGNITO_JWKS_URL", ""),
            honeypot=_bool("AGUI_HONEYPOT", "true"),
            session_ttl=_int("AGUI_SESSION_TTL", 604800),
            itsm_writeback_enabled=_bool("ITSM_WRITEBACK_ENABLED", "false"),
            require_webhook_auth=_bool("REQUIRE_WEBHOOK_AUTH", "false"),
            s3_bucket=_str("AGUI_S3_BUCKET", "agui-receipts"),
            dynamodb_table=_str("AGUI_DYNAMODB_TABLE", "agui-state"),
            local_receipt_dir=_str("AGUI_LOCAL_RECEIPT_DIR", "/tmp/agui-receipts"),
            allowed_origins=_str("ALLOWED_ORIGINS", ""),
        )

        database = DatabaseConfig(
            database_url=_str("DATABASE_URL", ""),
            pool_size=_int("DATABASE_POOL_SIZE", 5),
            pool_overflow=_int("DATABASE_POOL_OVERFLOW", 5),
            pool_timeout=_int("DATABASE_POOL_TIMEOUT", 30),
            pool_recycle=_int("DATABASE_POOL_RECYCLE", 1800),
            echo=_bool("DATABASE_ECHO", "false"),
            ops_db_path=_str("OPS_DB_PATH", "eval/ops_intelligence.db"),
            ops_db_enabled=_bool("OPS_DB_ENABLED", "true"),
            ops_retention_receipts_days=_int("OPS_RETENTION_RECEIPTS_DAYS", 30),
            ops_retention_history_days=_int("OPS_RETENTION_HISTORY_DAYS", 90),
            ops_retention_safety_days=_int("OPS_RETENTION_SAFETY_DAYS", 30),
            ops_queue_max=_int("OPS_QUEUE_MAX", 2000),
            ops_batch_size=_int("OPS_BATCH_SIZE", 50),
            ops_batch_delay=_float("OPS_BATCH_DELAY", 1.0),
        )

        workers = WorkersConfig(
            aws_region=_str("AWS_REGION", "us-east-1"),
            agentcore_gateway_url=_str("AGENTCORE_GATEWAY_URL", ""),
            gateway_token_refresh_buffer_seconds=_int("GATEWAY_TOKEN_REFRESH_BUFFER_SECONDS", 600),
            mcp_call_timeout_seconds=_int("MCP_CALL_TIMEOUT_SECONDS", 30),
            mcp_max_retries=_int("MCP_MAX_RETRIES", 2),
            mcp_dedup_enabled=_bool("MCP_DEDUP_ENABLED", "false"),
            agentcore_target_moogsoft=_str("AGENTCORE_TARGET_MOOGSOFT", "MoogsoftTarget"),
            agentcore_target_splunk=_str("AGENTCORE_TARGET_SPLUNK", "SplunkTarget"),
            agentcore_target_sysdig=_str("AGENTCORE_TARGET_SYSDIG", "SysdigTarget"),
            agentcore_target_signalfx=_str("AGENTCORE_TARGET_SIGNALFX", "SignalFxTarget"),
            agentcore_target_dynatrace=_str("AGENTCORE_TARGET_DYNATRACE", "DynatraceTarget"),
            agentcore_target_servicenow=_str("AGENTCORE_TARGET_SERVICENOW", "ServiceNowTarget"),
            agentcore_target_github=_str("AGENTCORE_TARGET_GITHUB", "GitHubTarget"),
            agentcore_target_confluence=_str("AGENTCORE_TARGET_CONFLUENCE", "ConfluenceTarget"),
            agentcore_target_kubernetes=_str("AGENTCORE_TARGET_KUBERNETES", "KubernetesTarget"),
            tool_discovery_ttl_seconds=_int("TOOL_DISCOVERY_TTL_SECONDS", 300),
            enable_thousandeyes_rca=_bool("ENABLE_THOUSANDEYES_RCA", "false"),
            te_use_fixtures=_bool("TE_USE_FIXTURES", "false"),
            te_mcp_url=_str("TE_MCP_URL", "http://localhost:8004"),
            te_timeout=_int("TE_TIMEOUT", 10),
            visual_evidence_enabled=_bool("VISUAL_EVIDENCE_ENABLED", "true"),
            visual_evidence_fetch_images=_bool("VISUAL_EVIDENCE_FETCH_IMAGES", "false"),
            git_worker_enabled=_bool("GIT_WORKER_ENABLED", "true"),
            git_bisect_max_commits=_int("GIT_BISECT_MAX_COMMITS", 50),
            code_worker_min_confidence=_int("CODE_WORKER_MIN_CONFIDENCE", 60),
            code_worker_max_diff_chars=_int("CODE_WORKER_MAX_DIFF_CHARS", 8000),
        )

        integrations = IntegrationsConfig(
            slack_rca_channel=_str("SLACK_RCA_CHANNEL", "#incidents"),
            slack_intel_channel=_str("SLACK_INTEL_CHANNEL", "#sre-intelligence"),
            notify_timeout_sec=_int("NOTIFY_TIMEOUT_SEC", 8),
            notify_min_confidence=_float("NOTIFY_MIN_CONFIDENCE", 0.0),
            opsgenie_api_url=_str("OPSGENIE_API_URL", "https://api.opsgenie.com/v2"),
            tenant_config_path=_str("TENANT_CONFIG_PATH", "config/tenants.yaml"),
            default_org_id=_str("DEFAULT_ORG_ID", "default"),
        )

        return cls(
            supervisor=supervisor,
            intelligence=intelligence,
            agui=agui,
            database=database,
            workers=workers,
            integrations=integrations,
            environment=_str("ENVIRONMENT", "development"),
            log_level=_str("LOG_LEVEL", "INFO"),
        )

    def validate(self) -> None:
        """Raise ValueError for obviously invalid config combinations.

        Keeps the check list short — only catches misconfigurations that would
        cause silent wrong behaviour rather than an obvious crash.
        """
        errors: list[str] = []

        if not 0.0 < self.supervisor.loop_convergence_threshold <= 1.0:
            errors.append(
                f"LOOP_CONVERGENCE_THRESHOLD={self.supervisor.loop_convergence_threshold} "
                "must be in (0, 1]"
            )
        if self.supervisor.loop_max_nudges < 0:
            errors.append(
                f"LOOP_MAX_NUDGES={self.supervisor.loop_max_nudges} must be >= 0"
            )
        if self.supervisor.investigation_budget_max_calls < 1:
            errors.append(
                f"INVESTIGATION_BUDGET_MAX_CALLS={self.supervisor.investigation_budget_max_calls} "
                "must be >= 1"
            )
        if self.agui.auth_required and not os.environ.get("AGUI_JWT_SECRET"):
            errors.append(
                "AGUI_AUTH_REQUIRED=true but AGUI_JWT_SECRET is not set"
            )
        if self.workers.enable_thousandeyes_rca and not os.environ.get("TE_TOKEN"):
            errors.append(
                "ENABLE_THOUSANDEYES_RCA=true but TE_TOKEN is not set"
            )
        if self.environment not in ("development", "staging", "production"):
            errors.append(
                f"ENVIRONMENT={self.environment!r} must be development, staging, or production"
            )

        if errors:
            raise ValueError(
                "SentinelConfig validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_config: Optional[SentinelConfig] = None


def get_config() -> SentinelConfig:
    """Return the process-level SentinelConfig, building it on first call."""
    global _config
    if _config is None:
        _config = SentinelConfig.from_env()
    return _config


def reset_config() -> None:
    """Clear the cached config so the next get_config() call re-reads env.

    Call this in test teardown after monkeypatching env vars.
    """
    global _config
    _config = None
