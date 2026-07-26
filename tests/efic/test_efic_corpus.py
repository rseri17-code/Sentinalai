"""EFIC — Enterprise Failure Intelligence Corpus tests.

Validates the corpus is deterministic, EB-0-runnable, deduplicated by reasoning
problem, and that every scenario satisfies the quality gates (known ground
truth, >=2 required MCPs, negative evidence, red herrings, reasoning contract).
Coverage is asserted honestly — gaps are reported, not hidden.
"""
from __future__ import annotations

import json
import os

from eval.efic.build_corpus import MCPS, TAXONOMY, build_corpus
from enterprisebench.loader import validate_corpus
from enterprisebench.runner import NOT_MEASURED, run

_CORPUS_JSON = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "eval", "efic", "corpus.json")


class TestDeterminismAndReuse:
    def test_deterministic_rebuild(self):
        assert json.dumps(build_corpus(), sort_keys=True) == \
            json.dumps(build_corpus(), sort_keys=True)

    def test_committed_file_matches_build(self):
        with open(_CORPUS_JSON) as f:
            committed = json.load(f)
        assert committed == build_corpus()

    def test_eic_compatible_content_addressed(self):
        for e in build_corpus()["corpus"]:
            assert len(e["task"]["task_hash"]) == 16
            # replay seed is the content-addressed task hash (deterministic)
            assert e["efic"]["replay_seed"] == e["task"]["task_hash"]

    def test_eb0_validates_and_runs(self):
        c = build_corpus()
        assert len(validate_corpus(c)) == c["coverage"]["scenarios"]
        # runs through EB-0 offline; no engine submissions -> NOT_MEASURED
        report = run(_CORPUS_JSON)
        assert report["counts"][NOT_MEASURED] == report["scenario_count"]


class TestDeduplication:
    def test_no_duplicate_reasoning_problem(self):
        ent = build_corpus()["corpus"]
        keys = [(e["efic"]["failure_family"], e["efic"]["failure_mode"],
                 e["efic"]["reasoning_category"]) for e in ent]
        assert len(keys) == len(set(keys))     # every scenario is distinct

    def test_unique_scenario_ids(self):
        ids = [e["task"]["task_id"] for e in build_corpus()["corpus"]]
        assert len(ids) == len(set(ids))


class TestQualityGates:
    def test_every_scenario_has_known_ground_truth(self):
        for e in build_corpus()["corpus"]:
            gt = e["task"]["ground_truth"]
            assert gt["root_cause"] and gt["root_cause_service"]
            assert gt["necessary_evidence"] and gt["decisive_evidence"]

    def test_cross_mcp_at_least_two_required(self):
        for e in build_corpus()["corpus"]:
            required = [m for m, u in e["efic"]["mcp_utilization"].items()
                        if u == "required"]
            assert len(required) >= 2, e["efic"]["scenario_id"]

    def test_negative_evidence_and_red_herrings_present(self):
        for e in build_corpus()["corpus"]:
            assert e["efic"]["negative_evidence"], e["efic"]["scenario_id"]
            assert e["efic"]["red_herrings"], e["efic"]["scenario_id"]

    def test_reasoning_contract_present(self):
        for e in build_corpus()["corpus"]:
            ef = e["efic"]
            assert ef["hypotheses_considered"] and ef["hypotheses_eliminated"]
            assert ef["reasoning_category"] and ef["contributing_factors"]

    def test_confidence_range_and_owner_recommendation(self):
        for e in build_corpus()["corpus"]:
            lo, hi = e["efic"]["expected_confidence_range"]
            assert 0 <= lo <= hi <= 100
            assert e["efic"]["expected_owner"] and e["efic"]["expected_recommendation"]

    def test_mcp_utilization_values_valid(self):
        allowed = {"required", "optional", "expected_empty", "not_applicable"}
        for e in build_corpus()["corpus"]:
            u = e["efic"]["mcp_utilization"]
            assert set(u) == set(MCPS)               # every MCP declared
            assert set(u.values()) <= allowed


class TestCoverage:
    def test_coverage_model_present_and_honest(self):
        c = build_corpus()
        cov = c["coverage"]
        n = len(c["corpus"])
        assert cov["scenarios"] == n
        # families covered are a subset of the taxonomy
        assert set(cov["failure_families_covered"]).issubset(set(TAXONOMY))
        # gaps are reported (not hidden) — the corpus is not yet complete
        assert cov["failure_modes_gaps"], "gaps must be reported honestly"
        # every scenario carries negative evidence + red herrings (count-agnostic)
        assert cov["scenarios_with_negative_evidence"] == n
        assert cov["scenarios_with_red_herrings"] == n

    def test_taxonomy_modes_are_distinct(self):
        for fam, modes in TAXONOMY.items():
            assert len(modes) == len(set(modes)), fam

    def test_all_families_covered(self):
        cov = build_corpus()["coverage"]
        assert set(cov["failure_families_covered"]) == set(TAXONOMY)

    def test_reasoning_record_fields_present(self):
        for e in build_corpus()["corpus"]:
            ef = e["efic"]
            assert ef["coverage_contribution"]
            # expected_queries covers every required MCP
            required = {m for m, u in ef["mcp_utilization"].items() if u == "required"}
            assert set(ef["expected_queries"]) == required
            assert "supporting_evidence" in ef


class TestInvestigationSpec:
    """EFIC-3: every reasoning case defines the expected investigation process,
    not just the answer. The spec is derived deterministically and lives only in
    the hidden efic block (task hashes / EB-0 grading are unaffected)."""

    _SPEC_KEYS = {
        "observed_symptoms", "evidence_attribution", "hypothesis_graph",
        "confidence_evolution", "mcp_investigation_contract", "business_context",
        "operational_context", "blast_radius", "escalation_boundary",
        "recovery_verification", "postmortem_summary",
    }

    def test_every_scenario_has_full_spec(self):
        for e in build_corpus()["corpus"]:
            spec = e["efic"]["investigation_spec"]
            assert self._SPEC_KEYS.issubset(spec), e["efic"]["scenario_id"]
            # symptom mirrors the incident the operator actually sees
            assert spec["observed_symptoms"] == e["task"]["incident"]["summary"]

    def test_evidence_attribution_classified(self):
        allowed = {"primary", "supporting", "red_herring", "negative"}
        for e in build_corpus()["corpus"]:
            attr = e["efic"]["investigation_spec"]["evidence_attribution"]
            assert attr, e["efic"]["scenario_id"]
            assert {a["class"] for a in attr} <= allowed
            # the decisive signal is attributed as primary
            assert any(a["class"] == "primary" for a in attr), e["efic"]["scenario_id"]

    def test_hypothesis_graph_resolves_to_a_survivor(self):
        for e in build_corpus()["corpus"]:
            g = e["efic"]["investigation_spec"]["hypothesis_graph"]
            considered = e["efic"]["hypotheses_considered"]
            elim_names = [d["hypothesis"] for d in g["eliminated"]]
            assert g["final"] in considered, e["efic"]["scenario_id"]
            assert g["final"] not in elim_names, e["efic"]["scenario_id"]
            assert set(g["initial"]) == set(considered)
            for d in g["eliminated"]:
                assert d["hypothesis"] and "eliminated_by" in d

    def test_confidence_evolution_bounded_and_terminal(self):
        for e in build_corpus()["corpus"]:
            lo, hi = e["efic"]["expected_confidence_range"]
            evo = e["efic"]["investigation_spec"]["confidence_evolution"]
            assert len(evo) >= 4, e["efic"]["scenario_id"]
            assert all(lo <= s["confidence"] <= hi for s in evo), e["efic"]["scenario_id"]
            # never overclaims: starts at the low bound, ends confirmed at the high bound
            assert evo[0]["confidence"] == lo
            assert evo[-1]["confidence"] == hi

    def test_mcp_contract_covers_required_sources(self):
        for e in build_corpus()["corpus"]:
            required = {m for m, u in e["efic"]["mcp_utilization"].items()
                        if u == "required"}
            contract = e["efic"]["investigation_spec"]["mcp_investigation_contract"]
            assert {c["mcp"] for c in contract} == required, e["efic"]["scenario_id"]
            # the decisive source carries primary importance
            assert any(c["evidence_importance"] == "primary" for c in contract)

    def test_operational_and_blast_context_consistent(self):
        for e in build_corpus()["corpus"]:
            spec = e["efic"]["investigation_spec"]
            gt = e["task"]["ground_truth"]
            oc = spec["operational_context"]
            assert oc["root_cause_service"] == gt["root_cause_service"]
            # cross-service incidents record the origin as a dependency
            cross = oc["affected_service"] != oc["root_cause_service"]
            assert bool(oc["dependencies"]) == cross, e["efic"]["scenario_id"]
            assert spec["blast_radius"]["origin_service"] == gt["root_cause_service"]
            assert spec["postmortem_summary"] and spec["recovery_verification"]["remediation"]
