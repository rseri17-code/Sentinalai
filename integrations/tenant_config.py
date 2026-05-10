"""Multi-tenant organisation configuration for SentinalAI.

Each org (tenant) has its own:
  - Notification channel overrides (Slack channel, PD service key, etc.)
  - Threshold overrides (critique_threshold, min_confidence_to_act, …)
  - Investigation settings (max_rounds, skip_weight_threshold, etc.)
  - Allowed services filter (restrict investigations to specific services)

Config resolution order (highest wins):
  1. Per-org JSON config file: TENANT_CONFIG_PATH (default: eval/tenants.json)
  2. Per-org environment variables: TENANT_{ORG_ID}_{KEY}=value
  3. Global defaults (adaptive thresholds or hard-coded defaults)

Thread-safe: config file is read at startup and cached; hot-reload via
  reload() or by sending SIGHUP (in production).

Usage:
    from integrations.tenant_config import get_tenant_config, TenantConfig

    cfg = get_tenant_config("acme-corp")
    channel = cfg.slack_channel          # "#sre-acme" or global default
    thr = cfg.threshold("critique_threshold")  # org override or adaptive default
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("sentinalai.tenant_config")

TENANT_CONFIG_PATH = os.environ.get(
    "TENANT_CONFIG_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "tenants.json"),
)

_DEFAULT_ORG = os.environ.get("DEFAULT_ORG_ID", "default")

_lock = threading.RLock()
_cache: dict[str, "TenantConfig"] = {}
_raw: dict[str, Any] = {}
_loaded = False


# ---------------------------------------------------------------------------
# Tenant config dataclass
# ---------------------------------------------------------------------------

@dataclass
class TenantConfig:
    org_id: str

    # Notification overrides
    slack_channel: str = ""
    slack_mention: str = ""           # e.g. "@sre-oncall"
    pagerduty_service_key: str = ""
    opsgenie_team: str = ""
    servicenow_assignment_group: str = ""

    # Threshold overrides (None = use global adaptive value)
    threshold_overrides: dict[str, float] = field(default_factory=dict)

    # Investigation settings
    max_rounds: int = 0               # 0 = global default
    min_confidence_to_act: float = 0.0  # 0 = global default
    allowed_services: list[str] = field(default_factory=list)  # empty = all services

    # Feature flags per tenant
    enable_remediation: bool = True
    enable_k8s_actions: bool = False  # off by default — requires explicit opt-in
    enable_postmortem_auto_draft: bool = True
    notify_min_confidence: float = 50.0

    # Raw extras for forward-compatibility
    _extras: dict[str, Any] = field(default_factory=dict, repr=False)

    def threshold(self, name: str) -> float:
        """Return threshold value: org override → adaptive default → hard default."""
        if name in self.threshold_overrides:
            return float(self.threshold_overrides[name])
        try:
            from supervisor.adaptive_thresholds import get as _at_get
            return _at_get(name)
        except Exception:
            _HARD_DEFAULTS = {
                "critique_threshold": 0.62,
                "store_quality_threshold": 0.60,
                "skip_weight_threshold": 0.35,
                "min_confidence_to_act": 30.0,
            }
            return _HARD_DEFAULTS.get(name, 0.5)

    def allows_service(self, service: str) -> bool:
        """Return True if this org is allowed to investigate the given service."""
        if not self.allowed_services:
            return True
        return service.lower() in {s.lower() for s in self.allowed_services}

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "slack_channel": self.slack_channel,
            "slack_mention": self.slack_mention,
            "pagerduty_service_key": bool(self.pagerduty_service_key),
            "opsgenie_team": self.opsgenie_team,
            "servicenow_assignment_group": self.servicenow_assignment_group,
            "threshold_overrides": self.threshold_overrides,
            "max_rounds": self.max_rounds,
            "min_confidence_to_act": self.min_confidence_to_act,
            "allowed_services": self.allowed_services,
            "enable_remediation": self.enable_remediation,
            "enable_k8s_actions": self.enable_k8s_actions,
            "enable_postmortem_auto_draft": self.enable_postmortem_auto_draft,
            "notify_min_confidence": self.notify_min_confidence,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tenant_config(org_id: str = "") -> TenantConfig:
    """Return the TenantConfig for the given org (or the default org)."""
    oid = org_id or _DEFAULT_ORG
    with _lock:
        _ensure_loaded()
        if oid not in _cache:
            _cache[oid] = _build_config(oid)
        return _cache[oid]


def list_tenants() -> list[str]:
    """Return all configured org IDs."""
    with _lock:
        _ensure_loaded()
        return list(_raw.keys())


def upsert_tenant(org_id: str, settings: dict[str, Any]) -> TenantConfig:
    """Create or update a tenant config entry and persist it."""
    with _lock:
        _ensure_loaded()
        _raw[org_id] = {**_raw.get(org_id, {}), **settings}
        _cache[org_id] = _build_config(org_id)
        _save()
        return _cache[org_id]


def reload() -> None:
    """Hot-reload config from disk (e.g. after file change or SIGHUP)."""
    with _lock:
        global _loaded
        _loaded = False
        _cache.clear()
        _raw.clear()
        _ensure_loaded()
    logger.info("Tenant config reloaded from %s", TENANT_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    try:
        with open(TENANT_CONFIG_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _raw.update(data)
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Tenant config parse error (%s): %s", TENANT_CONFIG_PATH, exc)
    _loaded = True


def _build_config(org_id: str) -> TenantConfig:
    """Build a TenantConfig from raw dict + env var overrides."""
    raw = dict(_raw.get(org_id, {}))
    env_prefix = f"TENANT_{org_id.upper().replace('-', '_')}_"

    # Env var overrides for string fields
    _str_fields = [
        "slack_channel", "slack_mention", "pagerduty_service_key",
        "opsgenie_team", "servicenow_assignment_group",
    ]
    for f in _str_fields:
        env_val = os.environ.get(f"{env_prefix}{f.upper()}", "")
        if env_val:
            raw[f] = env_val

    # Float env overrides
    _float_fields = ["min_confidence_to_act", "notify_min_confidence"]
    for f in _float_fields:
        env_val = os.environ.get(f"{env_prefix}{f.upper()}", "")
        if env_val:
            try:
                raw[f] = float(env_val)
            except ValueError:
                pass

    extras = {k: v for k, v in raw.items() if k not in TenantConfig.__dataclass_fields__}

    return TenantConfig(
        org_id=org_id,
        slack_channel=raw.get("slack_channel", ""),
        slack_mention=raw.get("slack_mention", ""),
        pagerduty_service_key=raw.get("pagerduty_service_key", ""),
        opsgenie_team=raw.get("opsgenie_team", ""),
        servicenow_assignment_group=raw.get("servicenow_assignment_group", ""),
        threshold_overrides=raw.get("threshold_overrides", {}),
        max_rounds=int(raw.get("max_rounds", 0)),
        min_confidence_to_act=float(raw.get("min_confidence_to_act", 0.0)),
        allowed_services=raw.get("allowed_services", []),
        enable_remediation=bool(raw.get("enable_remediation", True)),
        enable_k8s_actions=bool(raw.get("enable_k8s_actions", False)),
        enable_postmortem_auto_draft=bool(raw.get("enable_postmortem_auto_draft", True)),
        notify_min_confidence=float(raw.get("notify_min_confidence", 50.0)),
        _extras=extras,
    )


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(TENANT_CONFIG_PATH), exist_ok=True)
        tmp = TENANT_CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_raw, f, indent=2)
        os.replace(tmp, TENANT_CONFIG_PATH)
    except Exception as exc:
        logger.warning("Tenant config save failed: %s", exc)
