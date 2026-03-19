"""AG UI Receipts API.

Routes:
  GET  /api/v1/investigations/{id}/receipts        → List all receipts
  GET  /api/v1/investigations/{id}/receipts/{rid}  → Get specific receipt
"""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, status

from agui.middleware.auth import ActorContext, get_actor
from agui.state_store import get_state_store
from agui.receipt_store import get_receipt_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/investigations", tags=["receipts"])


@router.get("/{investigation_id}/receipts")
async def list_receipts(
    investigation_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """
    List all receipts for an investigation.

    Receipts are the evidence records for every tool call made.
    Each receipt maps 1:1 to a node in the execution graph.
    """
    store = get_state_store()
    receipts = await store.get_receipts(investigation_id)
    return {
        "receipts": [r.model_dump() for r in receipts],
        "total": len(receipts),
        "investigation_id": investigation_id,
    }


@router.get("/{investigation_id}/receipts/{receipt_id}")
async def get_receipt(
    investigation_id: str,
    receipt_id: str,
    actor: ActorContext = Depends(get_actor),
):
    """
    Get a specific receipt by ID.

    Checks in-memory state store first, then S3/local receipt store.
    Verifies payload hash on retrieval.
    """
    # Try state store first (fast path)
    store = get_state_store()
    receipts = await store.get_receipts(investigation_id)
    for receipt in receipts:
        if receipt.receipt_id == receipt_id:
            # Verify hash integrity
            computed = receipt.compute_hash()
            if receipt.payload_hash and computed != receipt.payload_hash:
                logger.warning(
                    "Receipt %s hash mismatch: stored=%s computed=%s",
                    receipt_id, receipt.payload_hash[:16], computed[:16]
                )
            return receipt.model_dump()

    # Fall back to receipt store (S3/local)
    receipt_store = get_receipt_store()
    receipt = await receipt_store.get_receipt(investigation_id, receipt_id)
    if not receipt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Receipt {receipt_id} not found",
        )
    return receipt.model_dump()
