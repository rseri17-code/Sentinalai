"""End-to-end integration harnesses.

These tests wire *already-shipped* components (intel_memory, hypotheses,
strategy_optimizer, causal_graph, continuous_learning, models,
SentinelBench, SentinelReplay) into a single deterministic flow. They
add no new product capability — every assertion checks that the four
capabilities compose without contract drift.
"""
