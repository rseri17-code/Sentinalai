"""AG UI Receipt Store — S3 with local filesystem fallback.

Receipts are immutable evidence records. Once written, never mutated.

S3 structure:
  s3://{bucket}/receipts/{investigation_id}/{receipt_id}.json
  s3://{bucket}/replays/{investigation_id}/snapshot.json

Design principles:
- Immutability: S3 Object Lock (Compliance mode for regulated environments)
- Versioning: S3 versioning enabled
- Integrity: SHA256 hash in receipt + S3 object metadata
- Lifecycle: 1-year active → Glacier after 90 days → Deep Archive after 1 year
- Cost: ~$0.023/GB/month (standard), ~$0.004/GB/month (Glacier)
"""
from __future__ import annotations

import json
import logging
import os
import hashlib
from pathlib import Path
from typing import Any, Optional

from agui.schemas.receipts import UIReceipt

logger = logging.getLogger(__name__)

S3_BUCKET = os.getenv("AGUI_S3_BUCKET", "agui-receipts")
LOCAL_RECEIPT_DIR = os.getenv("AGUI_LOCAL_RECEIPT_DIR", "/tmp/agui-receipts")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


class S3ReceiptStore:
    """S3-backed receipt store."""

    def __init__(self, bucket: str = S3_BUCKET, region: str = AWS_REGION) -> None:
        self.bucket = bucket
        self.region = region
        self._client = None
        self._available = False
        self._init_client()

    def _init_client(self) -> None:
        try:
            import boto3
            self._client = boto3.client("s3", region_name=self.region)
            # Quick connectivity check
            self._client.head_bucket(Bucket=self.bucket)
            self._available = True
            logger.info("S3 receipt store initialized (bucket=%s)", self.bucket)
        except Exception as e:
            logger.warning("S3 unavailable: %s — using local fallback", e)
            self._available = False

    def _receipt_key(self, investigation_id: str, receipt_id: str) -> str:
        return f"receipts/{investigation_id}/{receipt_id}.json"

    def _replay_key(self, investigation_id: str) -> str:
        return f"replays/{investigation_id}/snapshot.json"

    async def put_receipt(self, receipt: UIReceipt) -> Optional[str]:
        """Store receipt and return S3 URI."""
        if not self._available:
            return None
        try:
            key = self._receipt_key(receipt.investigation_id, receipt.receipt_id)
            body = receipt.model_dump_json().encode()
            checksum = hashlib.sha256(body).hexdigest()
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                Metadata={
                    "investigation-id": receipt.investigation_id,
                    "receipt-id": receipt.receipt_id,
                    "trace-id": receipt.trace_id,
                    "sha256": checksum,
                    "schema-version": receipt.schema_version,
                },
                ChecksumSHA256=checksum,
            )
            uri = f"s3://{self.bucket}/{key}"
            logger.debug("Receipt stored: %s", uri)
            return uri
        except Exception as e:
            logger.error("S3 put_receipt failed: %s", e)
            return None

    async def get_receipt(self, investigation_id: str, receipt_id: str) -> Optional[UIReceipt]:
        if not self._available:
            return None
        try:
            key = self._receipt_key(investigation_id, receipt_id)
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            body = resp["Body"].read()
            # Verify integrity
            actual_hash = hashlib.sha256(body).hexdigest()
            expected_hash = resp.get("Metadata", {}).get("sha256", "")
            if expected_hash and actual_hash != expected_hash:
                logger.error("Receipt integrity check failed: %s", receipt_id)
                return None
            return UIReceipt.model_validate_json(body)
        except Exception as e:
            logger.error("S3 get_receipt failed: %s", e)
            return None

    async def put_replay_snapshot(
        self, investigation_id: str, snapshot: dict[str, Any]
    ) -> Optional[str]:
        if not self._available:
            return None
        try:
            key = self._replay_key(investigation_id)
            body = json.dumps(snapshot, default=str).encode()
            checksum = hashlib.sha256(body).hexdigest()
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                Metadata={"sha256": checksum},
            )
            return f"s3://{self.bucket}/{key}"
        except Exception as e:
            logger.error("S3 put_replay_snapshot failed: %s", e)
            return None

    async def get_replay_snapshot(self, investigation_id: str) -> Optional[dict[str, Any]]:
        if not self._available:
            return None
        try:
            key = self._replay_key(investigation_id)
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            body = resp["Body"].read()
            # Integrity check
            actual_hash = hashlib.sha256(body).hexdigest()
            expected_hash = resp.get("Metadata", {}).get("sha256", "")
            if expected_hash and actual_hash != expected_hash:
                logger.error("Replay snapshot integrity check failed: %s", investigation_id)
                return None
            return json.loads(body)
        except Exception as e:
            logger.error("S3 get_replay_snapshot failed: %s", e)
            return None


class LocalReceiptStore:
    """Local filesystem fallback for development."""

    def __init__(self, base_dir: str = LOCAL_RECEIPT_DIR) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _receipt_path(self, investigation_id: str, receipt_id: str) -> Path:
        d = self.base_dir / "receipts" / investigation_id
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{receipt_id}.json"

    def _replay_path(self, investigation_id: str) -> Path:
        d = self.base_dir / "replays" / investigation_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "snapshot.json"

    async def put_receipt(self, receipt: UIReceipt) -> Optional[str]:
        path = self._receipt_path(receipt.investigation_id, receipt.receipt_id)
        path.write_text(receipt.model_dump_json(indent=2))
        return str(path)

    async def get_receipt(self, investigation_id: str, receipt_id: str) -> Optional[UIReceipt]:
        path = self._receipt_path(investigation_id, receipt_id)
        if not path.exists():
            return None
        try:
            return UIReceipt.model_validate_json(path.read_text())
        except Exception as e:
            logger.error("LocalReceiptStore get_receipt failed: %s", e)
            return None

    async def put_replay_snapshot(
        self, investigation_id: str, snapshot: dict[str, Any]
    ) -> Optional[str]:
        path = self._replay_path(investigation_id)
        path.write_text(json.dumps(snapshot, default=str, indent=2))
        return str(path)

    async def get_replay_snapshot(self, investigation_id: str) -> Optional[dict[str, Any]]:
        path = self._replay_path(investigation_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception as e:
            logger.error("LocalReceiptStore get_replay_snapshot failed: %s", e)
            return None


class ReceiptStore:
    """Facade — tries S3 first, falls back to local."""

    def __init__(self) -> None:
        self._s3 = S3ReceiptStore()
        self._local = LocalReceiptStore()
        self._use_s3 = self._s3._available

    async def put_receipt(self, receipt: UIReceipt) -> str:
        uri = None
        if self._use_s3:
            uri = await self._s3.put_receipt(receipt)
        if not uri:
            uri = await self._local.put_receipt(receipt) or ""
        return uri

    async def get_receipt(self, investigation_id: str, receipt_id: str) -> Optional[UIReceipt]:
        receipt = await self._local.get_receipt(investigation_id, receipt_id)
        if receipt:
            return receipt
        return await self._s3.get_receipt(investigation_id, receipt_id)

    async def put_replay_snapshot(
        self, investigation_id: str, snapshot: dict[str, Any]
    ) -> str:
        uri = None
        if self._use_s3:
            uri = await self._s3.put_replay_snapshot(investigation_id, snapshot)
        if not uri:
            uri = await self._local.put_replay_snapshot(investigation_id, snapshot) or ""
        return uri

    async def get_replay_snapshot(self, investigation_id: str) -> Optional[dict[str, Any]]:
        snapshot = await self._local.get_replay_snapshot(investigation_id)
        if snapshot:
            return snapshot
        return await self._s3.get_replay_snapshot(investigation_id)


# Global instance
_receipt_store: Optional[ReceiptStore] = None


def get_receipt_store() -> ReceiptStore:
    global _receipt_store
    if _receipt_store is None:
        _receipt_store = ReceiptStore()
    return _receipt_store
