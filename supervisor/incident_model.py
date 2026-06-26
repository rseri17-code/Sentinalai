# Re-exported from sentinel_core for backward compatibility.
# sentinel_core.models.incident is the canonical source.
from sentinel_core.models.incident import (  # noqa: F401
    Incident,
    _normalize_severity,
    _normalize_snow_state,
    _extract_pd_assignee,
    _SEVERITY_LABELS,
    _MOOGSOFT_STRING_MAP,
)

__all__ = [
    "Incident",
    "_normalize_severity",
    "_normalize_snow_state",
    "_extract_pd_assignee",
    "_SEVERITY_LABELS",
    "_MOOGSOFT_STRING_MAP",
]
