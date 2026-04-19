"""AG UI State Store — DynamoDB with in-memory fallback.

DynamoDB table design:
  Table: agui-state

  Primary key:
    pk (string) = INVESTIGATION#{investigation_id}
    sk (string) = STATE | EVENT#{seq}#{event_id} | RECEIPT#{seq}#{receipt_id} | CONTROL#{ts}#{action_id}

  GSI-1 (for incident lookup):
    gsi1pk = INCIDENT#{incident_id}
    gsi1sk = started_at (ISO 8601)

  GSI-2 (for status-based queries):
    gsi2pk = STATUS#{status}
    gsi2sk = started_at

  TTL attribute: ttl (Unix epoch, auto-deleted after expiry)

Failure modes:
  - DynamoDB unavailable: fall back to in-memory store
  - Credential error: log warning, fall back to in-memory
  - Put failure: log warning, continue (best-effort persistence)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, Protocol, runtime_checkable

from agui.schemas.events import AGUIEvent
from agui.schemas.incidents import IncidentState, ControlAction
from agui.schemas.receipts import UIReceipt

logger = logging.getLogger(__name__)

DYNAMODB_TABLE = os.getenv("AGUI_DYNAMODB_TABLE", "agui-state")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


@runtime_checkable
class StateBackend(Protocol):
    async def put_state(self, state: IncidentState) -> None: ...
    async def get_state(self, investigation_id: str) -> Optional[IncidentState]: ...
    async def put_event(self, event: AGUIEvent) -> None: ...
    async def get_events(self, investigation_id: str, since_seq: int) -> list[AGUIEvent]: ...
    async def put_receipt(self, receipt: UIReceipt) -> None: ...
    async def get_receipts(self, investigation_id: str) -> list[UIReceipt]: ...
    async def put_control(self, action: ControlAction) -> None: ...
    async def list_investigations(
        self, status: Optional[str], limit: int, offset: int
    ) -> list[IncidentState]: ...


class InMemoryStateBackend:
    """In-memory fallback when DynamoDB is unavailable."""

    def __init__(self) -> None:
        self._states: dict[str, IncidentState] = {}
        self._events: dict[str, list[AGUIEvent]] = {}
        self._receipts: dict[str, list[UIReceipt]] = {}
        self._controls: dict[str, list[ControlAction]] = {}

    async def put_state(self, state: IncidentState) -> None:
        self._states[state.investigation_id] = state

    async def get_state(self, investigation_id: str) -> Optional[IncidentState]:
        return self._states.get(investigation_id)

    async def put_event(self, event: AGUIEvent) -> None:
        inv_id = event.investigation_id
        if inv_id not in self._events:
            self._events[inv_id] = []
        self._events[inv_id].append(event)

    async def get_events(self, investigation_id: str, since_seq: int = 0) -> list[AGUIEvent]:
        events = self._events.get(investigation_id, [])
        return [e for e in events if e.sequence_num >= since_seq]

    async def put_receipt(self, receipt: UIReceipt) -> None:
        inv_id = receipt.investigation_id
        if inv_id not in self._receipts:
            self._receipts[inv_id] = []
        self._receipts[inv_id].append(receipt)

    async def get_receipts(self, investigation_id: str) -> list[UIReceipt]:
        return self._receipts.get(investigation_id, [])

    async def put_control(self, action: ControlAction) -> None:
        inv_id = action.investigation_id
        if inv_id not in self._controls:
            self._controls[inv_id] = []
        self._controls[inv_id].append(action)

    async def list_investigations(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentState]:
        states = list(self._states.values())
        if status:
            states = [s for s in states if s.status.value == status]
        states.sort(key=lambda s: s.started_at or "", reverse=True)
        return states[offset: offset + limit]


class DynamoDBStateBackend:
    """DynamoDB-backed state store."""

    def __init__(self, table_name: str = DYNAMODB_TABLE, region: str = AWS_REGION) -> None:
        self.table_name = table_name
        self.region = region
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        try:
            import boto3
            dynamodb = boto3.resource("dynamodb", region_name=self.region)
            self._table = dynamodb.Table(self.table_name)
            self._client = self._table
            self._available = True
            logger.info("DynamoDB state store initialized (table=%s)", self.table_name)
        except Exception as e:
            logger.warning("DynamoDB unavailable: %s — using in-memory fallback", e)
            self._available = False

    async def put_state(self, state: IncidentState) -> None:
        if not self._available:
            return
        try:
            item = state.to_dynamo()
            # DynamoDB doesn't accept nested Pydantic models
            item = json.loads(json.dumps(item, default=str))
            self._table.put_item(Item=item)
        except Exception as e:
            logger.error("DynamoDB put_state failed: %s", e)

    async def get_state(self, investigation_id: str) -> Optional[IncidentState]:
        if not self._available:
            return None
        try:
            resp = self._table.get_item(Key={
                "pk": f"INVESTIGATION#{investigation_id}",
                "sk": "STATE",
            })
            item = resp.get("Item")
            if item:
                item.pop("pk", None)
                item.pop("sk", None)
                item.pop("gsi1pk", None)
                item.pop("gsi1sk", None)
                item.pop("gsi2pk", None)
                item.pop("gsi2sk", None)
                return IncidentState(**item)
        except Exception as e:
            logger.error("DynamoDB get_state failed: %s", e)
        return None

    async def put_event(self, event: AGUIEvent) -> None:
        if not self._available:
            return
        try:
            item = event.to_dynamo()
            item = json.loads(json.dumps(item, default=str))
            self._table.put_item(Item=item)
        except Exception as e:
            logger.error("DynamoDB put_event failed: %s", e)

    async def get_events(self, investigation_id: str, since_seq: int = 0) -> list[AGUIEvent]:
        if not self._available:
            return []
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._table.query(
                KeyConditionExpression=(
                    Key("pk").eq(f"INVESTIGATION#{investigation_id}") &
                    Key("sk").begins_with(f"EVENT#{since_seq:08d}")
                ),
                ScanIndexForward=True,
            )
            items = resp.get("Items", [])
            events = []
            for item in items:
                item.pop("pk", None)
                item.pop("sk", None)
                try:
                    events.append(AGUIEvent(**item))
                except Exception:
                    pass
            return events
        except Exception as e:
            logger.error("DynamoDB get_events failed: %s", e)
        return []

    async def put_receipt(self, receipt: UIReceipt) -> None:
        if not self._available:
            return
        try:
            item = receipt.to_dynamo()
            item = json.loads(json.dumps(item, default=str))
            self._table.put_item(Item=item)
        except Exception as e:
            logger.error("DynamoDB put_receipt failed: %s", e)

    async def get_receipts(self, investigation_id: str) -> list[UIReceipt]:
        if not self._available:
            return []
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._table.query(
                KeyConditionExpression=(
                    Key("pk").eq(f"INVESTIGATION#{investigation_id}") &
                    Key("sk").begins_with("RECEIPT#")
                ),
                ScanIndexForward=True,
            )
            receipts = []
            for item in resp.get("Items", []):
                item.pop("pk", None)
                item.pop("sk", None)
                item.pop("gsi1pk", None)
                item.pop("gsi1sk", None)
                try:
                    receipts.append(UIReceipt(**item))
                except Exception:
                    pass
            return receipts
        except Exception as e:
            logger.error("DynamoDB get_receipts failed: %s", e)
        return []

    async def put_control(self, action: ControlAction) -> None:
        if not self._available:
            return
        try:
            item = action.to_dynamo()
            item = json.loads(json.dumps(item, default=str))
            self._table.put_item(Item=item)
        except Exception as e:
            logger.error("DynamoDB put_control failed: %s", e)

    async def list_investigations(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentState]:
        if not self._available:
            return []
        try:
            if status:
                from boto3.dynamodb.conditions import Key
                resp = self._table.query(
                    IndexName="gsi2",
                    KeyConditionExpression=Key("gsi2pk").eq(f"STATUS#{status}"),
                    ScanIndexForward=False,
                    Limit=limit + offset,
                )
            else:
                resp = self._table.scan(
                    FilterExpression="sk = :s",
                    ExpressionAttributeValues={":s": "STATE"},
                    Limit=limit + offset,
                )
            items = resp.get("Items", [])[offset:]
            states = []
            for item in items:
                for key in ("pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk"):
                    item.pop(key, None)
                try:
                    states.append(IncidentState(**item))
                except Exception:
                    pass
            return states
        except Exception as e:
            logger.error("DynamoDB list_investigations failed: %s", e)
        return []


class StateStore:
    """
    State store facade — auto-selects backend based on environment.

    Falls back to in-memory if DynamoDB unavailable.
    """

    def __init__(self) -> None:
        self._dynamo = DynamoDBStateBackend()
        self._memory = InMemoryStateBackend()
        self._use_dynamo = self._dynamo._available

    def _backend(self) -> StateBackend:
        return self._dynamo if self._use_dynamo else self._memory

    async def put_state(self, state: IncidentState) -> None:
        await self._backend().put_state(state)
        await self._memory.put_state(state)  # Always cache in memory for fast reads

    async def get_state(self, investigation_id: str) -> Optional[IncidentState]:
        # Try memory cache first
        state = await self._memory.get_state(investigation_id)
        if state:
            return state
        # Fall back to DynamoDB
        return await self._dynamo.get_state(investigation_id)

    async def put_event(self, event: AGUIEvent) -> None:
        await self._backend().put_event(event)
        await self._memory.put_event(event)

    async def get_events(self, investigation_id: str, since_seq: int = 0) -> list[AGUIEvent]:
        events = await self._memory.get_events(investigation_id, since_seq)
        if events:
            return events
        return await self._dynamo.get_events(investigation_id, since_seq)

    async def put_receipt(self, receipt: UIReceipt) -> None:
        await self._backend().put_receipt(receipt)
        await self._memory.put_receipt(receipt)

    async def get_receipts(self, investigation_id: str) -> list[UIReceipt]:
        receipts = await self._memory.get_receipts(investigation_id)
        if receipts:
            return receipts
        return await self._dynamo.get_receipts(investigation_id)

    async def put_control(self, action: ControlAction) -> None:
        await self._backend().put_control(action)

    async def list_investigations(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[IncidentState]:
        # Try memory first for running investigations
        states = await self._memory.list_investigations(status, limit, offset)
        if states:
            return states
        return await self._dynamo.list_investigations(status, limit, offset)


# Global instance
_store: Optional[StateStore] = None


def get_state_store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store
