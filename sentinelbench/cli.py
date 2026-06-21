import argparse
import json
import sys
from sentinelbench.loader import ScenarioLoader
from sentinelbench.runner import BenchRunner
from sentinelbench.baseline import BaselineComparator
from sentinelbench.schema import ScoreCard


def cmd_run(args):
    loader = ScenarioLoader()
    runner = BenchRunner(ci_mode=True)
    scenario, expected, alert, evidence = loader.load(args.scenario_dir)
    score = runner.run_scenario_with_fixture(scenario, expected, alert, evidence)
    print(json.dumps(score.model_dump(), indent=2))


def cmd_score(args):
    runner = BenchRunner(ci_mode=True)
    card = runner.run_all(args.scenarios_root)
    print(json.dumps(card.model_dump(), indent=2))


def cmd_compare(args):
    with open(args.result_a) as f:
        run_a = ScoreCard(**json.load(f))
    with open(args.result_b) as f:
        run_b = ScoreCard(**json.load(f))
    comparator = BaselineComparator()
    report = comparator.compare(run_a, run_b)
    import dataclasses
    print(json.dumps(dataclasses.asdict(report), indent=2))


def main():
    parser = argparse.ArgumentParser(prog="sentinelbench")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run")
    p_run.add_argument("scenario_dir")

    p_score = sub.add_parser("score")
    p_score.add_argument("scenarios_root")

    p_compare = sub.add_parser("compare")
    p_compare.add_argument("result_a")
    p_compare.add_argument("result_b")

    args = parser.parse_args()
    if args.command == "run":
        cmd_run(args)
    elif args.command == "score":
        cmd_score(args)
    elif args.command == "compare":
        cmd_compare(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
