"""Blast Radius API — compute and return blast radius for a proposed fix.

GET /api/v1/investigations/{investigation_id}/blast-radius
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger("sentinalai.agui.blast_radius")

router = APIRouter(tags=["blast_radius"])


@router.get("/api/v1/investigations/{investigation_id}/blast-radius")
async def get_blast_radius(investigation_id: str) -> JSONResponse:
    """Return the blast radius report for an investigation's proposed fix."""
    try:
        # Pull investigation result to extract service + fix type
        rca_result = await _fetch_rca(investigation_id)
        service = rca_result.get("affected_service", "unknown")
        fix_type = _infer_fix_type(rca_result)

        # Pull CMDB topology
        cmdb_topology: dict = {}
        kg_edges: list = []
        try:
            from workers.itsm_worker import ItsmWorker
            itsm = ItsmWorker()
            cmdb_result = itsm.execute("get_cmdb_context", {"service": service, "depth": 3})
            cmdb_topology = cmdb_result.get("topology", {})
        except Exception:
            pass

        from supervisor.blast_radius import compute_blast_radius

        report = compute_blast_radius(
            target_service=service,
            fix_type=fix_type,
            cmdb_topology=cmdb_topology,
            kg_edges=kg_edges,
        )

        return JSONResponse(_report_to_dict(report))

    except Exception as exc:
        logger.exception("Failed to compute blast radius for %s: %s", investigation_id, exc)
        return JSONResponse(
            {
                "target_service": "unknown",
                "fix_type": "unknown",
                "risk_tier": "MEDIUM",
                "safe_to_auto_apply": False,
                "affected_service_count": 0,
                "p1_dependency_count": 0,
                "affected_services": [],
                "precautions": ["Blast radius could not be computed — proceed with caution"],
                "reasoning": f"Computation error: {exc}",
                "generated_at": "",
            }
        )


async def _fetch_rca(investigation_id: str) -> dict:
    try:
        from database.persistence import load_investigation
        result = load_investigation(investigation_id)
        if result:
            return result
    except Exception:
        pass
    return {}


def _infer_fix_type(rca_result: dict) -> str:
    remediation = rca_result.get("remediation", {})
    fix = remediation.get("permanent_fix", "").lower()
    if any(k in fix for k in ("restart", "redeploy", "rollout")):
        return "restart"
    if any(k in fix for k in ("config", "env", "configmap", "secret")):
        return "config_change"
    if any(k in fix for k in ("scale", "replica", "hpa")):
        return "scale_up"
    if "rollback" in fix:
        return "rollback"
    return "config_change"


def _report_to_dict(report) -> dict:
    from dataclasses import asdict
    try:
        d = asdict(report)
        # Ensure risk_tier is a string (not an enum)
        if hasattr(d.get("risk_tier"), "name"):
            d["risk_tier"] = d["risk_tier"].name
        for svc in d.get("affected_services", []):
            if hasattr(svc.get("risk_tier"), "name"):
                svc["risk_tier"] = svc["risk_tier"].name
        return d
    except Exception:
        return {
            "target_service": getattr(report, "target_service", "?"),
            "fix_type": getattr(report, "fix_type", "?"),
            "risk_tier": str(getattr(report, "risk_tier", "MEDIUM")),
            "safe_to_auto_apply": getattr(report, "safe_to_auto_apply", False),
            "affected_service_count": getattr(report, "affected_service_count", 0),
            "p1_dependency_count": getattr(report, "p1_dependency_count", 0),
            "affected_services": [],
            "precautions": list(getattr(report, "precautions", [])),
            "reasoning": getattr(report, "reasoning", ""),
            "generated_at": getattr(report, "generated_at", ""),
        }
