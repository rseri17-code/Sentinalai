#!/usr/bin/env python3
"""Report whether each worker's MCP tools are reachable, stubbed, missing, or failed.

Usage:
    python scripts/mcp_diagnostics.py
    python scripts/mcp_diagnostics.py --json

Text prints a worker line, then one indented row per tool. The worker
state is the worst required tool (failed > missing > stubbed > reachable).
JSON has ``workers`` and ``tools`` (sorted by worker, then tool).

Exit 0 when every required worker is reachable or stubbed.
Exit 2 when any required worker is missing or failed.
Secrets and credentials are never printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workers.mcp_diagnostics import diagnose, format_json, format_text, report_exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SentinalAI MCP connection diagnostics")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Print JSON")
    args = parser.parse_args(argv)
    report = diagnose()
    if args.json_output:
        sys.stdout.write(format_json(report))
    else:
        sys.stdout.write(format_text(report))
    return report_exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
