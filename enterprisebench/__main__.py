"""EB-0 CLI. Uses only the stdlib argparse (no new dependency).

    python -m enterprisebench run [--scenario ID] [--subset a,b,c]
        [--submissions PATH] [--out DIR] [--baseline PATH] [--fail-on-regression]
    python -m enterprisebench baseline create [--submissions PATH] --output PATH

Exit codes:
    0  successful run (no regression)
    1  evaluation regression vs baseline
    2  invalid corpus / configuration
    3  execution error
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from enterprisebench import (
    EB_VERSION,
    EXIT_ERROR,
    EXIT_INVALID_CORPUS,
    EXIT_OK,
    EXIT_REGRESSION,
)
from enterprisebench.baseline import compare
from enterprisebench.loader import CorpusError
from enterprisebench.report import baseline_summary, render_markdown, write_reports
from enterprisebench.runner import file_provider, no_engine_provider, run


def _provider(submissions_path: str | None):
    if not submissions_path:
        return no_engine_provider
    with open(submissions_path) as f:
        subs = json.load(f)
    return file_provider(subs)


def _summary_line(report: dict[str, Any]) -> str:
    c = report["counts"]
    return (f"EnterpriseBench v{report['enterprisebench_version']}  "
            f"scenarios={report['scenario_count']}  "
            f"PASS={c['PASS']} FAIL={c['FAIL']} NOT_MEASURED={c['NOT_MEASURED']} "
            f"UNSUPPORTED={c['UNSUPPORTED']} SKIPPED={c['SKIPPED']} "
            f"ERROR={c['ERROR']}  det_ok={report['scorer_determinism_ok']}  "
            f"replay={report['replay_status']}")


def cmd_run(args) -> int:
    try:
        only = None
        if args.scenario:
            only = [args.scenario]
        elif args.subset:
            only = [s.strip() for s in args.subset.split(",") if s.strip()]
        report = run(args.corpus, provider=_provider(args.submissions),
                     only=only, commit=args.commit or "",
                     run_timestamp=args.timestamp or "")
    except CorpusError as ex:
        print(f"INVALID CORPUS: {ex}", file=sys.stderr)
        return EXIT_INVALID_CORPUS
    except Exception as ex:  # execution error
        print(f"EXECUTION ERROR: {ex}", file=sys.stderr)
        return EXIT_ERROR

    if args.out:
        paths = write_reports(report, args.out)
        if args.markdown:
            with open(paths["report"].replace(".json", ".md"), "w") as f:
                f.write(render_markdown(report))
    print(_summary_line(report))

    if args.baseline:
        try:
            with open(args.baseline) as f:
                base = json.load(f)
        except Exception as ex:
            print(f"INVALID CONFIG: cannot read baseline: {ex}", file=sys.stderr)
            return EXIT_INVALID_CORPUS
        cmp = compare(report, base)
        if cmp["has_regression"]:
            print("REGRESSIONS:", file=sys.stderr)
            for r in cmp["regressions"]:
                print(f"  - {r}", file=sys.stderr)
            if args.fail_on_regression:
                return EXIT_REGRESSION
    return EXIT_OK


def cmd_baseline_create(args) -> int:
    if not args.output:
        print("INVALID CONFIG: --output is required", file=sys.stderr)
        return EXIT_INVALID_CORPUS
    try:
        report = run(args.corpus, provider=_provider(args.submissions))
    except CorpusError as ex:
        print(f"INVALID CORPUS: {ex}", file=sys.stderr)
        return EXIT_INVALID_CORPUS
    except Exception as ex:
        print(f"EXECUTION ERROR: {ex}", file=sys.stderr)
        return EXIT_ERROR
    # explicit creation only — never auto-updates an existing baseline elsewhere
    with open(args.output, "w") as f:
        json.dump(baseline_summary(report), f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"baseline written: {args.output}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="enterprisebench",
                                description=f"EnterpriseBench EB-0 v{EB_VERSION}")
    p.add_argument("--corpus", default=None, help="corpus path (default: eval/enterprise/corpus.json)")
    p.add_argument("--submissions", default=None, help="JSON {task_id: submission} (default: none → NOT_MEASURED)")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the corpus and report")
    r.add_argument("--scenario", default=None)
    r.add_argument("--subset", default=None, help="comma-separated scenario ids")
    r.add_argument("--out", default=None, help="output directory for reports")
    r.add_argument("--markdown", action="store_true")
    r.add_argument("--baseline", default=None, help="baseline_summary.json to compare against")
    r.add_argument("--fail-on-regression", action="store_true")
    r.add_argument("--commit", default=None)
    r.add_argument("--timestamp", default=None)
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("baseline", help="baseline operations")
    bsub = b.add_subparsers(dest="baseline_cmd", required=True)
    bc = bsub.add_parser("create", help="explicitly create a baseline")
    bc.add_argument("--output", required=True)
    bc.set_defaults(func=cmd_baseline_create)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
