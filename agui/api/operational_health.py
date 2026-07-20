"""AG UI Operational Health API — the OIP convergence endpoint.

Routes:
  GET /api/v1/operational-health → per-service health rolled up from completed
      investigations, with a per-service drill-down link to the supporting
      investigation, and an honest signal-coverage disclosure.

This is the first runtime exposure of the Operational Intelligence layer. It
composes the frozen ``sentinel_core.oip.operational_health`` service over real
completed investigations (via ``agui.oip_adapter``). It duplicates no
investigation logic, invents no evidence, and reads only what the pipeline
already produced.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from agui.middleware.auth import ActorContext, get_actor
from agui.oip_adapter import signal_coverage, states_to_oip_inputs
from agui.state_store import get_state_store
from sentinel_core.oip import operational_health

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/operational-health", tags=["operational-health"])


@router.get("")
async def get_operational_health(
    limit: int = Query(200, ge=1, le=1000),
    actor: ActorContext = Depends(get_actor),
):
    """Estate operational health composed from completed investigations.

    Returns the operational_health rollup plus:
      * ``drilldown`` — {service: investigation_id} to open the supporting
        investigation for each service.
      * ``signal_coverage`` — which health-score inputs are actually present
        (honest: a low score with absent validation signals means "unmeasured",
        not "failing").
    """
    store = get_state_store()
    states = await store.list_investigations(status="completed", limit=limit,
                                             offset=0)
    results, incidents, drilldown = states_to_oip_inputs(states)

    health = operational_health(results, incidents)
    return {
        **health,
        "drilldown": drilldown,
        "signal_coverage": signal_coverage(results),
    }
