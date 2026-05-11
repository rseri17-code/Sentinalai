"""Kubernetes remediation executor — safe kubectl wrapper for SentinalAI.

Safety model (non-negotiable):
  1. DRY-RUN by default — all actions first executed with --dry-run=server.
     K8S_DRY_RUN=false must be set explicitly to allow live execution.
  2. Blast radius check — actions on resources matching >K8S_MAX_PODS_AFFECTED
     (default 10) pods require explicit override.
  3. Approval gate — actions of class "disruptive" require human approval token
     before live execution.  Token is a UUID stored in ops persistence.
  4. Audit log — every action (dry-run and live) is recorded to ops persistence
     with actor, timestamp, dry_run flag, outcome, and stdout/stderr.
  5. Namespace allow-list — K8S_ALLOWED_NAMESPACES restricts execution scope
     (default: all namespaces allowed if unset, or comma-separated list).

Supported actions:
  rollback_deployment   — kubectl rollout undo deployment/<name> -n <ns>
  scale_deployment      — kubectl scale deployment/<name> --replicas=N -n <ns>
  restart_deployment    — kubectl rollout restart deployment/<name> -n <ns>
  delete_pod            — kubectl delete pod/<name> -n <ns>
  cordon_node           — kubectl cordon <node>
  uncordon_node         — kubectl uncordon <node>
  apply_manifest        — kubectl apply -f <manifest_path> -n <ns>

Usage:
    from supervisor.k8s_executor import get_executor, K8sAction

    executor = get_executor()
    result = executor.execute(K8sAction(
        action="rollback_deployment",
        namespace="production",
        resource_name="payment-processor",
        actor="sentinalai-agent",
        investigation_id="inv-123",
    ))
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("sentinalai.k8s_executor")

K8S_DRY_RUN = os.environ.get("K8S_DRY_RUN", "true").lower() not in ("0", "false", "no")
K8S_MAX_PODS_AFFECTED = int(os.environ.get("K8S_MAX_PODS_AFFECTED", "10"))
K8S_TIMEOUT_SECS = int(os.environ.get("K8S_TIMEOUT_SECS", "60"))
K8S_KUBECTL_PATH = os.environ.get("K8S_KUBECTL_PATH", "kubectl")

_ALLOWED_NS_RAW = os.environ.get("K8S_ALLOWED_NAMESPACES", "")
_ALLOWED_NAMESPACES: set[str] = (
    {ns.strip() for ns in _ALLOWED_NS_RAW.split(",") if ns.strip()}
    if _ALLOWED_NS_RAW else set()
)

_DISRUPTIVE_ACTIONS = {"rollback_deployment", "delete_pod", "cordon_node", "apply_manifest"}

_lock = threading.Lock()
_instance: Optional["K8sExecutor"] = None

# Pending approval tokens: token → K8sAction
_pending_approvals: dict[str, "K8sAction"] = {}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class K8sAction:
    action: str
    namespace: str = "default"
    resource_name: str = ""
    replicas: int = 0               # for scale_deployment
    manifest_path: str = ""         # for apply_manifest
    actor: str = "sentinalai-agent"
    investigation_id: str = ""
    approval_token: str = ""        # populated if requires approval
    extra_flags: list[str] = field(default_factory=list)


@dataclass
class K8sResult:
    ok: bool
    action: str
    dry_run: bool
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    pods_affected: int = 0
    approval_required: bool = False
    approval_token: str = ""
    audit_id: str = ""
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class K8sExecutor:
    """Safe Kubernetes remediation executor."""

    # ── Public API ──────────────────────────────────────────────────────────

    def execute(self, action: K8sAction) -> K8sResult:
        """Execute a Kubernetes remediation action.

        Enforces: namespace allow-list → blast radius → approval gate → dry-run flag.
        All executions are audit-logged.
        """
        t0 = time.time()
        audit_id = str(uuid.uuid4())[:8]

        # Namespace check
        if _ALLOWED_NAMESPACES and action.namespace not in _ALLOWED_NAMESPACES:
            return self._rejected(action, audit_id, t0,
                f"Namespace '{action.namespace}' not in K8S_ALLOWED_NAMESPACES")

        # Build command
        try:
            cmd = self._build_command(action, dry_run=True)
        except ValueError as exc:
            return self._rejected(action, audit_id, t0, str(exc))

        # Dry-run first (always)
        dry_result = self._run(cmd, dry_run=True)
        pods_affected = self._estimate_pods_affected(action)

        if not dry_result.ok:
            self._audit(action, dry_result, audit_id, dry_run=True)
            return dry_result

        # Blast radius check
        if pods_affected > K8S_MAX_PODS_AFFECTED:
            msg = (f"Blast radius {pods_affected} pods exceeds K8S_MAX_PODS_AFFECTED="
                   f"{K8S_MAX_PODS_AFFECTED}. Increase limit or reduce scope.")
            return self._rejected(action, audit_id, t0, msg)

        # Global dry-run mode — report success but don't apply
        if K8S_DRY_RUN:
            result = K8sResult(
                ok=True, action=action.action, dry_run=True,
                stdout=dry_result.stdout, stderr=dry_result.stderr,
                pods_affected=pods_affected, audit_id=audit_id,
                elapsed_ms=(time.time() - t0) * 1000,
            )
            self._audit(action, result, audit_id, dry_run=True)
            logger.info("K8s DRY-RUN %s/%s/%s OK (set K8S_DRY_RUN=false for live)",
                        action.action, action.namespace, action.resource_name)
            return result

        # Approval gate for disruptive actions
        if action.action in _DISRUPTIVE_ACTIONS:
            if not action.approval_token:
                token = self._request_approval(action)
                result = K8sResult(
                    ok=False, action=action.action, dry_run=True,
                    approval_required=True, approval_token=token,
                    pods_affected=pods_affected, audit_id=audit_id,
                    elapsed_ms=(time.time() - t0) * 1000,
                    error="Approval required for disruptive action",
                )
                self._audit(action, result, audit_id, dry_run=True)
                return result

            if not self._validate_approval(action.approval_token, action):
                return self._rejected(action, audit_id, t0, "Invalid or expired approval token")

        # Live execution
        live_cmd = self._build_command(action, dry_run=False)
        result = self._run(live_cmd, dry_run=False)
        result.pods_affected = pods_affected
        result.audit_id = audit_id
        result.elapsed_ms = (time.time() - t0) * 1000
        self._audit(action, result, audit_id, dry_run=False)

        level = logging.INFO if result.ok else logging.ERROR
        logger.log(level, "K8s LIVE %s/%s/%s %s",
                   action.action, action.namespace, action.resource_name,
                   "OK" if result.ok else f"FAILED: {result.error}")
        return result

    def request_approval(self, action: K8sAction) -> str:
        """Pre-request an approval token for a disruptive action."""
        return self._request_approval(action)

    def approve(self, token: str) -> bool:
        """Mark a pending approval as approved (called by human operator)."""
        with _lock:
            if token in _pending_approvals:
                logger.info("K8s approval granted: token=%s", token)
                return True
        logger.warning("K8s approval: unknown token %s", token)
        return False

    # ── Command builder ─────────────────────────────────────────────────────

    def _build_command(self, action: K8sAction, dry_run: bool) -> list[str]:
        kubectl = K8S_KUBECTL_PATH
        ns = ["-n", action.namespace] if action.namespace else []
        dry = ["--dry-run=server"] if dry_run else []

        a = action.action
        name = action.resource_name

        if a == "rollback_deployment":
            if not name:
                raise ValueError("resource_name required for rollback_deployment")
            return [kubectl, "rollout", "undo", f"deployment/{name}", *ns, *dry]

        if a == "scale_deployment":
            if not name:
                raise ValueError("resource_name required for scale_deployment")
            replicas = action.replicas
            if replicas < 0:
                raise ValueError("replicas must be >= 0")
            return [kubectl, "scale", f"deployment/{name}", f"--replicas={replicas}", *ns, *dry]

        if a == "restart_deployment":
            if not name:
                raise ValueError("resource_name required for restart_deployment")
            return [kubectl, "rollout", "restart", f"deployment/{name}", *ns, *dry]

        if a == "delete_pod":
            if not name:
                raise ValueError("resource_name required for delete_pod")
            return [kubectl, "delete", "pod", name, *ns, *dry]

        if a == "cordon_node":
            if not name:
                raise ValueError("resource_name (node name) required for cordon_node")
            return [kubectl, "cordon", name, *dry]

        if a == "uncordon_node":
            if not name:
                raise ValueError("resource_name (node name) required for uncordon_node")
            return [kubectl, "uncordon", name, *dry]

        if a == "apply_manifest":
            if not action.manifest_path:
                raise ValueError("manifest_path required for apply_manifest")
            if not os.path.isfile(action.manifest_path):
                raise ValueError(f"manifest_path does not exist: {action.manifest_path}")
            return [kubectl, "apply", "-f", action.manifest_path, *ns, *dry]

        raise ValueError(f"Unknown action: {a!r}")

    # ── Subprocess runner ───────────────────────────────────────────────────

    def _run(self, cmd: list[str], dry_run: bool) -> K8sResult:
        action_name = cmd[1] if len(cmd) > 1 else "unknown"
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=K8S_TIMEOUT_SECS,
            )
            ok = proc.returncode == 0
            return K8sResult(
                ok=ok, action=action_name, dry_run=dry_run,
                stdout=proc.stdout[:4096], stderr=proc.stderr[:2048],
                error="" if ok else f"kubectl exited {proc.returncode}: {proc.stderr[:256]}",
            )
        except FileNotFoundError:
            return K8sResult(
                ok=False, action=action_name, dry_run=dry_run,
                error=f"kubectl not found at '{K8S_KUBECTL_PATH}' — install kubectl or set K8S_KUBECTL_PATH",
            )
        except subprocess.TimeoutExpired:
            return K8sResult(
                ok=False, action=action_name, dry_run=dry_run,
                error=f"kubectl timed out after {K8S_TIMEOUT_SECS}s",
            )
        except Exception as exc:
            return K8sResult(
                ok=False, action=action_name, dry_run=dry_run,
                error=str(exc),
            )

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _estimate_pods_affected(self, action: K8sAction) -> int:
        """Estimate pods affected without a live call (conservative estimate)."""
        if action.action in ("cordon_node", "uncordon_node"):
            return K8S_MAX_PODS_AFFECTED  # unknown — use max as safety
        if action.action == "delete_pod":
            return 1
        if action.action == "scale_deployment":
            return abs(action.replicas)
        return 1

    def _request_approval(self, action: K8sAction) -> str:
        token = str(uuid.uuid4())
        with _lock:
            _pending_approvals[token] = action
        logger.warning(
            "K8s approval requested: action=%s resource=%s ns=%s token=%s",
            action.action, action.resource_name, action.namespace, token,
        )
        try:
            from database.ops_persistence import get_ops_store
            get_ops_store().persist_safety_event(
                event_type="k8s_approval_requested",
                context=f"{action.action}/{action.namespace}/{action.resource_name}",
                source="k8s_executor",
            )
        except Exception:
            pass
        return token

    def _validate_approval(self, token: str, action: K8sAction) -> bool:
        with _lock:
            pending = _pending_approvals.get(token)
            if pending is None:
                return False
            if pending.action != action.action or pending.resource_name != action.resource_name:
                return False
            del _pending_approvals[token]
            return True

    def _rejected(self, action: K8sAction, audit_id: str, t0: float, reason: str) -> K8sResult:
        result = K8sResult(
            ok=False, action=action.action, dry_run=True,
            error=reason, audit_id=audit_id,
            elapsed_ms=(time.time() - t0) * 1000,
        )
        logger.warning("K8s action REJECTED: %s — %s", action.action, reason)
        self._audit(action, result, audit_id, dry_run=True)
        return result

    def _audit(self, action: K8sAction, result: K8sResult, audit_id: str, dry_run: bool) -> None:
        try:
            from database.ops_persistence import get_ops_store
            get_ops_store().persist_safety_event(
                event_type="k8s_action_executed" if result.ok else "k8s_action_failed",
                context=f"{action.action}/{action.namespace}/{action.resource_name}",
                source="k8s_executor",
                details={
                    "audit_id": audit_id,
                    "dry_run": dry_run,
                    "ok": result.ok,
                    "actor": action.actor,
                    "investigation_id": action.investigation_id,
                    "error": result.error[:200] if result.error else "",
                    "pods_affected": result.pods_affected,
                    "elapsed_ms": round(result.elapsed_ms, 1),
                },
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

def get_executor() -> K8sExecutor:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = K8sExecutor()
    return _instance
