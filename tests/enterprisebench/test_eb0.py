"""EnterpriseBench EB-0 tests.

Cover the runner/loader/report/baseline over small fixtures (not the full
corpus): deterministic ordering, duplicate/malformed/unsupported rejection,
scorer composition, NOT_MEASURED preservation, deterministic report
serialization, repeated-run equivalence, baseline comparison + regression exit
code, explicit baseline creation (no auto-mutation), scenario filtering, and
offline execution.
"""
from __future__ import annotations

import json

import pytest

from enterprisebench import (
    EXIT_INVALID_CORPUS,
    EXIT_OK,
    EXIT_REGRESSION,
    file_provider,
    no_engine_provider,
)
from enterprisebench.baseline import compare
from enterprisebench.loader import CorpusError, corpus_hash, validate_corpus
from enterprisebench.report import baseline_summary, content_hash, write_reports
from enterprisebench.runner import FAIL, NOT_MEASURED, PASS, SKIPPED, UNSUPPORTED, run
from sentinel_core.eic import make_submission, make_task


def _task(tid, cat="database"):
    return make_task(
        task_id=tid, category=cat, difficulty="single_cause",
        incident={"service": "svc", "severity": 1, "summary": "x"},
        telemetry={"logs": {"e": ["boom"]}},
        ground_truth={"root_cause": "db pool exhaustion",
                      "root_cause_keywords": ["pool", "exhaustion"],
                      "root_cause_service": "svc",
                      "necessary_evidence": ["logs"],
                      "decisive_evidence": ["logs"]})


def _corpus(*ids):
    return {"schema_version": 1, "corpus": [
        {"task": _task(i), "expected": {"owner": "t", "confidence_min": 60,
                                        "confidence_max": 90,
                                        "recommendation": "increase pool"}}
        for i in ids]}


def _good_submission(task):
    gt = task["ground_truth"]
    s = make_submission(engine="oracle", task_id=task["task_id"],
                        root_cause=gt["root_cause"],
                        localized_service=gt["root_cause_service"],
                        evidence_used=gt["necessary_evidence"],
                        decisive_evidence=gt["decisive_evidence"],
                        hypotheses=[gt["root_cause"]], proof="p", confidence=75)
    s["owner"] = "t"
    s["recommendation"] = "increase pool"
    return s


def _write(tmp_path, corpus):
    p = tmp_path / "corpus.json"
    p.write_text(json.dumps(corpus))
    return str(p)


class TestLoaderValidation:
    def test_deterministic_ordering(self):
        scn = validate_corpus(_corpus("B", "A", "C"))
        assert [s["task"]["task_id"] for s in scn] == ["A", "B", "C"]

    def test_duplicate_id_rejected(self):
        with pytest.raises(CorpusError):
            validate_corpus(_corpus("A", "A"))

    def test_missing_ground_truth_rejected(self):
        c = _corpus("A")
        c["corpus"][0]["task"]["ground_truth"]["root_cause"] = ""
        with pytest.raises(CorpusError):
            validate_corpus(c)

    def test_malformed_evidence_rejected(self):
        c = _corpus("A")
        c["corpus"][0]["task"]["ground_truth"]["necessary_evidence"] = []
        with pytest.raises(CorpusError):
            validate_corpus(c)

    def test_invalid_confidence_bounds_rejected(self):
        c = _corpus("A")
        c["corpus"][0]["expected"]["confidence_min"] = 95
        c["corpus"][0]["expected"]["confidence_max"] = 90
        with pytest.raises(CorpusError):
            validate_corpus(c)

    def test_unsupported_corpus_schema_rejected(self):
        with pytest.raises(CorpusError):
            validate_corpus({"schema_version": 999, "corpus": []})

    def test_corpus_hash_order_independent(self):
        assert corpus_hash(_corpus("A", "B")) == corpus_hash(_corpus("B", "A"))


class TestRunner:
    def test_not_measured_without_engine(self, tmp_path):
        report = run(_write(tmp_path, _corpus("A", "B")),
                     provider=no_engine_provider)
        assert report["counts"][NOT_MEASURED] == 2
        assert report["counts"][PASS] == 0
        assert report["replay_status"] == NOT_MEASURED  # engine replay = EB-2

    def test_scorer_composition_pass(self, tmp_path):
        c = _corpus("A")
        subs = {"A": _good_submission(c["corpus"][0]["task"])}
        report = run(_write(tmp_path, c), provider=file_provider(subs))
        r = report["scenarios"][0]
        assert r["outcome"] == PASS
        assert r["composite"] > 0.7
        assert r["dimensions"]["rca_correctness"]["state"] == "measured"
        assert r["expected_checks"]["owner_ok"] is True

    def test_wrong_submission_fails(self, tmp_path):
        c = _corpus("A")
        bad = make_submission(engine="weak", task_id="A",
                              root_cause="unrelated", localized_service="nope",
                              evidence_used=[], confidence=99)
        report = run(_write(tmp_path, c), provider=file_provider({"A": bad}))
        assert report["scenarios"][0]["outcome"] == FAIL

    def test_scenario_filtering(self, tmp_path):
        report = run(_write(tmp_path, _corpus("A", "B", "C")), only=["B"])
        outcomes = {r["scenario_id"]: r["outcome"] for r in report["scenarios"]}
        assert outcomes["A"] == SKIPPED and outcomes["C"] == SKIPPED
        assert outcomes["B"] == NOT_MEASURED

    def test_unsupported_task_schema(self, tmp_path):
        c = _corpus("A")
        c["corpus"][0]["task"]["schema_version"] = 999
        report = run(_write(tmp_path, c))
        assert report["scenarios"][0]["outcome"] == UNSUPPORTED

    def test_repeated_run_equivalence(self, tmp_path):
        c = _corpus("A")
        subs = {"A": _good_submission(c["corpus"][0]["task"])}
        p = _write(tmp_path, c)
        a = run(p, provider=file_provider(subs), run_timestamp="T1")
        b = run(p, provider=file_provider(subs), run_timestamp="T2")
        # volatile timestamp differs, but the content hash is identical
        assert content_hash(a) == content_hash(b)


class TestReportAndBaseline:
    def test_deterministic_serialization(self, tmp_path):
        c = _corpus("A")
        subs = {"A": _good_submission(c["corpus"][0]["task"])}
        report = run(_write(tmp_path, c), provider=file_provider(subs))
        paths = write_reports(report, str(tmp_path / "out"))
        first = (tmp_path / "out" / "enterprisebench_report.json").read_text()
        write_reports(report, str(tmp_path / "out2"))
        second = (tmp_path / "out2" / "enterprisebench_report.json").read_text()
        assert first == second
        assert set(paths) == {"report", "scenarios", "baseline"}

    def test_baseline_no_regression(self, tmp_path):
        c = _corpus("A")
        subs = {"A": _good_submission(c["corpus"][0]["task"])}
        report = run(_write(tmp_path, c), provider=file_provider(subs))
        base = baseline_summary(report)
        assert compare(report, base)["has_regression"] is False

    def test_baseline_detects_pass_to_fail(self, tmp_path):
        c = _corpus("A")
        good = {"A": _good_submission(c["corpus"][0]["task"])}
        p = _write(tmp_path, c)
        base = baseline_summary(run(p, provider=file_provider(good)))
        bad = make_submission(engine="weak", task_id="A", root_cause="x",
                              localized_service="y", evidence_used=[])
        now = run(p, provider=file_provider({"A": bad}))
        cmp = compare(now, base)
        assert cmp["has_regression"] is True
        assert any(r["kind"] == "pass_to_fail" for r in cmp["regressions"])


class TestCLI:
    def test_run_exit_ok(self, tmp_path):
        from enterprisebench.__main__ import main
        assert main(["--corpus", _write(tmp_path, _corpus("A")), "run",
                     "--out", str(tmp_path / "o")]) == EXIT_OK

    def test_invalid_corpus_exit_code(self, tmp_path):
        from enterprisebench.__main__ import main
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"schema_version": 999, "corpus": []}))
        from enterprisebench import EXIT_INVALID_CORPUS as EIC
        assert main(["--corpus", str(p), "run"]) == EIC

    def test_regression_exit_code(self, tmp_path):
        from enterprisebench.__main__ import main
        c = _corpus("A")
        good = {"A": _good_submission(c["corpus"][0]["task"])}
        cp = _write(tmp_path, c)
        subp = tmp_path / "subs.json"
        subp.write_text(json.dumps(good))
        base_path = tmp_path / "base.json"
        # explicit baseline creation
        assert main(["--corpus", cp, "--submissions", str(subp),
                     "baseline", "create", "--output", str(base_path)]) == EXIT_OK
        assert base_path.exists()
        # now regress with a bad submission and fail on regression
        bad = make_submission(engine="weak", task_id="A", root_cause="x",
                              localized_service="y", evidence_used=[])
        badp = tmp_path / "bad.json"
        badp.write_text(json.dumps({"A": bad}))
        rc = main(["--corpus", cp, "--submissions", str(badp), "run",
                   "--baseline", str(base_path), "--fail-on-regression"])
        assert rc == EXIT_REGRESSION

    def test_baseline_not_auto_mutated(self, tmp_path):
        # a run must never write/replace a baseline; only 'baseline create' does
        from enterprisebench.__main__ import main
        c = _corpus("A")
        cp = _write(tmp_path, c)
        base_path = tmp_path / "base.json"
        base_path.write_text('{"sentinel": "untouched"}')
        main(["--corpus", cp, "run", "--baseline", str(base_path),
              "--out", str(tmp_path / "o")])
        assert json.loads(base_path.read_text()) == {"sentinel": "untouched"}


def test_real_corpus_loads_and_runs():
    # smoke: the committed enterprise corpus validates and runs offline
    report = run()  # default corpus; no engine → all NOT_MEASURED
    assert report["scenario_count"] >= 1
    assert report["counts"][NOT_MEASURED] == report["scenario_count"]
    assert report["corpus_hash"]
