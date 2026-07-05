"""SentinelReplay — offline continuous learning engine.

Sits on top of SentinelBench (`tests.synthetic`) and:
- replays historical investigations,
- measures quality over time,
- detects regressions and improvements,
- identifies recurring weaknesses,
- emits deterministic learning recommendations.

Zero production runtime coupling. Zero network. Zero LLM. Zero
external dependencies.
"""
