"""Base worker class for all SentinalAI workers."""

from typing import Any


class BaseWorker:
    """Base class providing common worker interface.

    Every worker exposes a single ``execute(action, params)`` method.
    Subclasses register handlers for specific actions; unknown actions
    return an empty dict.
    """

    def __init__(self):
        self._handlers: dict[str, Any] = {}

    def register(self, action: str, handler):
        """Register a handler function for an action name."""
        self._handlers[action] = handler

    def execute(self, action: str, params: dict | None = None) -> dict:
        """Dispatch *action* to the registered handler, or return ``{}``."""
        params = params or {}
        handler = self._handlers.get(action)
        if handler is None:
            return {}
        try:
            return handler(params)
        except Exception:
            return {}
