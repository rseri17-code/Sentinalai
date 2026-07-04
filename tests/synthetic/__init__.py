"""SentinelBench — offline synthetic incident evaluation harness.

Pure-Python, offline, deterministic, CI-friendly. No production
connectors, no network, no LLM invocation. All scoring happens locally
against scenario JSON files under ``scenarios/``.

Public entry points:
- :mod:`schemas`  — canonical Scenario dataclass + schema validation.
- :mod:`scoring`  — 8 scoring dimensions + weighted overall.
- :mod:`runner`   — scenario loader + single/all scenario execution.
- :mod:`report`   — deterministic JSON report renderer.
"""
