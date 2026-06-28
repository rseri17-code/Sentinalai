"""supervisor.helpers — pure, stateless utilities extracted from agent.py.

These modules hold only pure functions with no dependency on supervisor
state (no ``self``, no workers, no LLM, no receipts, no TLS). They are
safe to import from anywhere without pulling in the supervisor god-module.
"""
