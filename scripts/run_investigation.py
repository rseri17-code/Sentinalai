#!/usr/bin/env python3
"""Run a SentinalAI investigation from the command line.

Usage:
    python scripts/run_investigation.py INC12345
    python scripts/run_investigation.py INC12345 --json
    python scripts/run_investigation.py INC12345 --replay
    python scripts/run_investigation.py INC12345 -v
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.agent import SentinalAISupervisor


def run_investigation(
    incident_id: str,
    replay: bool = False,
    json_output: bool = False,
) -> int:
    """Run investigation and return exit code (0=success, 1=failure)."""
    replay_dir = os.getenv("SENTINALAI_REPLAY_DIR", "/tmp/sentinalai_replays")
    supervisor = SentinalAISupervisor(replay_dir=replay_dir)

    start = time.monotonic()

    try:
        result = supervisor.investigate(incident_id, replay=replay)
    except Exception as e:
        logging.getLogger("sentinalai").error("Investigation failed: %s", e)
        if json_output:
            print(json.dumps({"error": str(e), "incident_id": incident_id}))
        else:
            print(f"ERROR: {e}")
        return 1

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    if json_output:
        output = {**result, "elapsed_ms": elapsed_ms}
        print(json.dumps(output, indent=2, default=str))
    else:
        print(f"Incident:    {incident_id}")
        print(f"Root Cause:  {result.get('root_cause', 'N/A')}")
        print(f"Confidence:  {result.get('confidence', 0)}")
        print(f"Duration:    {elapsed_ms}ms")
        print(f"Reasoning:   {result.get('reasoning', 'N/A')[:200]}")

        timeline = result.get("evidence_timeline", [])
        if timeline:
            print(f"\nTimeline ({len(timeline)} events):")
            for entry in timeline[:10]:
                ts = entry.get("timestamp", "")
                desc = entry.get("description", entry.get("event", ""))
                print(f"  {ts}  {desc}")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SentinalAI investigation")
    parser.add_argument("incident_id", help="Incident ID to investigate (e.g. INC12345)")
    parser.add_argument("--replay", action="store_true", help="Load from replay cache")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sys.exit(run_investigation(args.incident_id, args.replay, args.json_output))


if __name__ == "__main__":
    main()
