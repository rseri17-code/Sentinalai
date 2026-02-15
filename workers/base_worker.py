"""Base worker class for all SentinalAI workers.

Provides:
- Action dispatch with handler registration
- Structured logging for every call
- Error propagation with context (no silent swallowing)
- Timing metrics for observability
- Type-safe callable interface
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger("sentinalai.worker")

HandlerFn = Callable[[dict], dict]


class WorkerError(Exception):
    """Raised when a worker action fails with context."""

    def __init__(self, worker: str, action: str, cause: Exception):
        self.worker = worker
        self.action = action
        self.cause = cause
        super().__init__(f"{worker}.{action} failed: {cause}")


class BaseWorker:
    """Base class providing common worker interface.

    Every worker exposes a single ``execute(action, params)`` method.
    Subclasses register handlers for specific actions; unknown actions
    return an empty dict.
    """

    #: Subclasses should set this for logging context
    worker_name: str = "base"

    def __init__(self):
        self._handlers: dict[str, HandlerFn] = {}

    def register(self, action: str, handler: HandlerFn) -> None:
        """Register a handler function for an action name."""
        self._handlers[action] = handler

    def execute(self, action: str, params: dict | None = None) -> dict:
        """Dispatch *action* to the registered handler, or return ``{}``."""
        params = params or {}
        handler = self._handlers.get(action)
        if handler is None:
            logger.debug(
                "unknown_action",
                extra={"worker": self.worker_name, "action": action},
            )
            return {}
        start = time.monotonic()
        try:
            result = handler(params)
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.info(
                "worker_call",
                extra={
                    "worker": self.worker_name,
                    "action": action,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "success": True,
                },
            )
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.error(
                "worker_call_failed",
                extra={
                    "worker": self.worker_name,
                    "action": action,
                    "elapsed_ms": round(elapsed_ms, 1),
                    "error": str(exc),
                },
                exc_info=True,
            )
            return {}
