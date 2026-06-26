# Re-exported from sentinel_core for backward compatibility.
# sentinel_core.models.events is the canonical source.
from sentinel_core.models.events import (  # noqa: F401
    CURRENT_SCHEMA_VERSION,
    EventType,
    AGUIEvent,
    EventSchema,
)

__all__ = ["CURRENT_SCHEMA_VERSION", "EventType", "AGUIEvent", "EventSchema"]
