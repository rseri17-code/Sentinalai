"""EB-0 reporting — deterministic, versioned, machine-readable artifacts.

Writes `enterprisebench_report.json`, `scenario_results.jsonl`, and
`baseline_summary.json`. Output ordering is deterministic and contains no
machine-specific absolute paths. A ``content_hash`` covers everything except
explicitly-excluded volatile fields (documented below).
"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from enterprisebench.loader import sha256_16

# Volatile fields excluded from the content hash + baseline comparison.
# Documented per the determinism rule: these vary run-to-run without indicating
# a behavioral change.
VOLATILE_FIELDS = ("run_timestamp", "runtime_ms", "commit")


def content_hash(report: Mapping[str, Any]) -> str:
    stable = {k: v for k, v in report.items() if k not in VOLATILE_FIELDS}
    return sha256_16(stable)


def baseline_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """The stable subset used for baseline comparison (no volatile fields)."""
    return {
        "enterprisebench_version": report.get("enterprisebench_version"),
        "corpus_hash": report.get("corpus_hash"),
        "scorer_version": report.get("scorer_version"),
        "pass_threshold": report.get("pass_threshold"),
        "scenario_count": report.get("scenario_count"),
        "counts": report.get("counts"),
        "scorer_determinism_ok": report.get("scorer_determinism_ok"),
        "replay_status": report.get("replay_status"),
        "content_hash": content_hash(report),
        "scenarios": {
            r["scenario_id"]: {
                "outcome": r["outcome"],
                "composite": r["composite"],
                "scorer_determinism": r["scorer_determinism"],
            } for r in report.get("scenarios", [])
        },
    }


def _dump(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def write_reports(report: Mapping[str, Any], out_dir: str) -> dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "report": os.path.join(out_dir, "enterprisebench_report.json"),
        "scenarios": os.path.join(out_dir, "scenario_results.jsonl"),
        "baseline": os.path.join(out_dir, "baseline_summary.json"),
    }
    _dump(paths["report"], report)
    # scenario_results.jsonl — one canonical line per scenario, deterministic order
    with open(paths["scenarios"], "w") as f:
        for r in report.get("scenarios", []):
            f.write(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n")
    _dump(paths["baseline"], baseline_summary(report))
    return paths


def render_markdown(report: Mapping[str, Any]) -> str:
    c = report.get("counts", {})
    lines = [
        f"# EnterpriseBench report (v{report.get('enterprisebench_version')})",
        "",
        f"- corpus_hash: `{report.get('corpus_hash')}`  ·  scorer v"
        f"{report.get('scorer_version')}  ·  commit: "
        f"`{report.get('commit') or 'n/a'}`",
        f"- scenarios: {report.get('scenario_count')}  ·  "
        f"PASS {c.get('PASS',0)} · FAIL {c.get('FAIL',0)} · "
        f"NOT_MEASURED {c.get('NOT_MEASURED',0)} · UNSUPPORTED "
        f"{c.get('UNSUPPORTED',0)} · SKIPPED {c.get('SKIPPED',0)} · "
        f"ERROR {c.get('ERROR',0)}",
        f"- scorer determinism ok: {report.get('scorer_determinism_ok')}  ·  "
        f"replay: {report.get('replay_status')} "
        f"({report.get('replay_reason','')})",
        f"- content_hash: `{content_hash(report)}`",
    ]
    if report.get("failures"):
        lines += ["", "## Failures"]
        lines += [f"- {f['scenario_id']}: {f['reason']}"
                  for f in report["failures"]]
    return "\n".join(lines) + "\n"


__all__ = ["content_hash", "baseline_summary", "write_reports",
           "render_markdown", "VOLATILE_FIELDS"]
