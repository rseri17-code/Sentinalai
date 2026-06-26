"""sentinel_core — zero-dependency shared models and ports for SentinalAI.

This package contains stable, shared dataclasses, enums, Pydantic models,
and Protocol interfaces that are consumed across supervisor/, intelligence/,
workers/, and agui/ without creating circular imports.

Dependency rule: sentinel_core imports NOTHING from supervisor, intelligence,
workers, or agui. It depends only on the Python stdlib and pydantic.

Import from the sub-modules directly:
    from sentinel_core.models.incident import Incident
    from sentinel_core.models.events import AGUIEvent, EventType
    from sentinel_core.models.dev_task import DevTask
"""
__version__ = "0.1.0"
