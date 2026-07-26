"""EB-2 CLI — run the Investigation Evaluation Pipeline over the EFIC corpus.

    python -m enterprisebench.pipeline run [--corpus PATH] [--only A,B] \
        [--threshold 0.70] [--out DIR] [--markdown] [--timeout 180]

Deterministic + offline. Writes ``eb2_report.json`` (+ optional ``eb2_report.md``).
Exit codes: 0 ok · 2 usage/IO error · 3 execution error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping

from enterprisebench.pipeline.run import run_corpus


def _markdown(report: Mapping[str, Any]) -> str:
    c = report.get("counts", {})
    cfg = report.get("evaluated_config", {})
    lines = [
        f"# EnterpriseBench EB-2 — Investigation Evaluation (v{report.get('eb2_version')})",
        "",
        f"- corpus_hash: `{report.get('corpus_hash')}`  ·  scorer v"
        f"{report.get('scorer_version')}  ·  content_hash: `{report.get('content_hash')}`",
        f"- scenarios: {report.get('scenario_count')}  ·  PASS {c.get('PASS',0)} · "
        f"FAIL {c.get('FAIL',0)} · NOT_MEASURED {c.get('NOT_MEASURED',0)} · "
        f"ERROR {c.get('ERROR',0)}",
        f"- mean EIC score: {report.get('mean_eic_score')}  ·  "
        f"mean investigation score: {report.get('mean_investigation_score')}",
        f"- evaluated config: reasoning stack ON, LLM refinement OFF, "
        f"cross-incident learning neutralized ({cfg.get('note','')})",
        "",
        "## Per-scenario",
        "",
        "| scenario | outcome | EIC | inv | rca | localized | unreachable req |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in report.get("scenarios", []):
        dims = s.get("eic_dimensions", {}) or {}
        lines.append(
            f"| {s.get('scenario_id')} | {s.get('outcome')} | "
            f"{s.get('eic_score')} | {s.get('investigation_score')} | "
            f"{dims.get('rca_correctness')} | {s.get('engine_localized_service') or '—'} | "
            f"{','.join(s.get('reachability',{}).get('required_unreachable',[])) or '—'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="enterprisebench.pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run EB-2 over the EFIC corpus")
    r.add_argument("--corpus", default=None)
    r.add_argument("--only", default=None, help="comma-separated scenario ids")
    r.add_argument("--threshold", type=float, default=0.70)
    r.add_argument("--timeout", type=int, default=180)
    r.add_argument("--out", default=None, help="output directory")
    r.add_argument("--markdown", action="store_true")
    args = p.parse_args(argv)

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    try:
        report = run_corpus(args.corpus, only=only, pass_threshold=args.threshold,
                            timeout=args.timeout)
    except FileNotFoundError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2
    except Exception as ex:  # execution error
        print(f"error: {ex}", file=sys.stderr)
        return 3

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "eb2_report.json"), "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
        if args.markdown:
            with open(os.path.join(args.out, "eb2_report.md"), "w") as f:
                f.write(_markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
