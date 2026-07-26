"""Isolated investigation worker — runs ONE investigation in a fresh interpreter.

Invoked as ``python -m enterprisebench.pipeline._isolated_worker <task.json>
<trace.json>``. Running in a subprocess guarantees empty in-memory state and no
background-write leakage between scenarios, which — together with the empty
learning-state paths and LLM-off configuration — makes every EB-2 run
deterministic and order-independent. Not part of the public API.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile


# (module, class) pairs whose __init__ has a single repo-anchored ``storage_path``
# default computed from dirname(__file__) at import time.
_REPO_ANCHORED_WRITERS = (
    ("intelligence.episodic_memory", "EpisodicMemory", "episodic_memory.jsonl"),
    ("intelligence.causal_graph", "CausalGraph", "causal_graph.jsonl"),
    ("intelligence.resolution_knowledge", "ResolutionKnowledge", "resolution_knowledge.jsonl"),
)


def _sandbox_repo_anchored_writers(state_dir: str) -> None:
    import importlib
    for mod_name, cls_name, fname in _REPO_ANCHORED_WRITERS:
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name)
            cls.__init__.__defaults__ = (os.path.join(state_dir, fname),)
        except Exception:
            pass


def main(argv: list[str]) -> int:
    task_path, out_path = argv[1], argv[2]
    with open(task_path) as f:
        task = json.load(f)

    # Configure the fixed evaluated environment BEFORE importing the engine.
    state_dir = tempfile.mkdtemp(prefix="eb2-worker-state-")
    from enterprisebench.pipeline.execute import (
        _run_in_process, configure_deterministic_offline_env)
    configure_deterministic_offline_env(state_dir, force=True)
    os.environ.setdefault("AGUI_AUTH_REQUIRED", "false")

    # Isolate the engine's repo-anchored side-effect writers (intelligence stores
    # whose default path is bound to the repo at import: episodic memory, causal
    # graph, resolution knowledge). Rebinding the single ``storage_path`` default in
    # THIS subprocess only — no engine source is modified — sends every such write
    # into the sandbox, so a run leaves the working tree untouched.
    _sandbox_repo_anchored_writers(state_dir)

    trace = _run_in_process(task)
    with open(out_path, "w") as f:
        json.dump(trace, f, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
