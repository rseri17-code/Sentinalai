"""Tool call transparency REST endpoints.

GET  /transparency/{investigation_id}/tool-calls        — paginated waterfall
GET  /transparency/{investigation_id}/tool-calls/{id}   — single receipt detail
GET  /transparency/{investigation_id}/evidence-atlas    — bipartite graph
GET  /transparency/{investigation_id}/causal-chain      — ordered signal chain
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/transparency", tags=["transparency"])


def _get_emitter():
    try:
        from supervisor.tool_transparency import get_emitter
        return get_emitter()
    except ImportError:
        return None


def _receipt_to_dict(r: Any) -> dict:
    d = r.to_dict()
    # Inline signal facts as plain dicts
    d["signal_facts"] = [{"category": f["category"], "text": f["text"], "weight": f["weight"]} for f in d.get("signal_facts", [])]
    d["hypothesis_deltas"] = [
        {"name": hd["name"], "score_before": hd["score_before"], "score_after": hd["score_after"], "delta": hd["score_after"] - hd["score_before"]}
        for hd in d.get("hypothesis_deltas", [])
    ]
    return d


@router.get("/{investigation_id}/tool-calls")
def list_tool_calls(
    investigation_id: str,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    phase: str | None = Query(default=None),
    status: str | None = Query(default=None),
    worker: str | None = Query(default=None),
):
    emitter = _get_emitter()
    if emitter is None:
        return {"items": [], "total": 0, "investigation_id": investigation_id}

    receipts = emitter.get_receipts(investigation_id)

    if phase:
        receipts = [r for r in receipts if r.phase == phase]
    if status:
        receipts = [r for r in receipts if r.status == status]
    if worker:
        receipts = [r for r in receipts if r.worker == worker]

    total = len(receipts)
    page = receipts[offset: offset + limit]

    return {
        "investigation_id": investigation_id,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [_receipt_to_dict(r) for r in page],
    }


@router.get("/{investigation_id}/tool-calls/{receipt_id}")
def get_tool_call(investigation_id: str, receipt_id: str):
    emitter = _get_emitter()
    if emitter is None:
        raise HTTPException(status_code=503, detail="Transparency emitter not available")

    receipt = emitter.get_receipt(investigation_id, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")

    return _receipt_to_dict(receipt)


@router.get("/{investigation_id}/evidence-atlas")
def get_evidence_atlas(investigation_id: str):
    emitter = _get_emitter()
    if emitter is None:
        return {"nodes": [], "edges": [], "total_receipts": 0}

    return emitter.get_evidence_atlas(investigation_id)


@router.get("/{investigation_id}/causal-chain")
def get_causal_chain(investigation_id: str):
    """Return an ordered list of tool calls sorted by time, annotated with signal density."""
    emitter = _get_emitter()
    if emitter is None:
        return {"chain": [], "investigation_id": investigation_id}

    receipts = emitter.get_receipts(investigation_id)
    chain = []
    for r in receipts:
        chain.append({
            "receipt_id": r.receipt_id,
            "worker": r.worker,
            "action": r.action,
            "phase": r.phase,
            "intent_summary": r.intent_summary,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "signal_count": r.signal_count,
            "noise_ratio": r.noise_ratio,
            "result_count": r.result_count,
            "confidence_delta": r.confidence_delta,
            "called_at_ms": r.called_at_ms,
        })

    return {"chain": chain, "investigation_id": investigation_id, "total": len(chain)}
