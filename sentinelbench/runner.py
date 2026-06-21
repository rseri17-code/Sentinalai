import uuid
from datetime import datetime, timezone
from sentinelbench.schema import ScoreCard, RCAScore
from sentinelbench.loader import ScenarioLoader
from sentinelbench.scorer import RCAScorer


class BenchRunner:
    def __init__(self, ci_mode: bool = False):
        self.ci_mode = ci_mode
        self._loader = ScenarioLoader()
        self._scorer = RCAScorer()

    def run_scenario_with_fixture(
        self,
        scenario,
        expected,
        alert: dict,
        evidence: dict,
    ) -> RCAScore:
        synthetic_result = self._build_fixture_result(scenario, evidence)
        evidence_used = list(evidence.keys())
        return self._scorer.score(scenario.scenario_id, synthetic_result, expected, evidence_used)

    def _build_fixture_result(self, scenario, evidence: dict) -> dict:
        text_parts = []
        for v in evidence.values():
            if isinstance(v, dict):
                text_parts.append(str(v))
            elif isinstance(v, list):
                text_parts.extend(str(x) for x in v[:3])
        root_cause_text = " ".join(text_parts)[:500]
        return {
            "root_cause": root_cause_text,
            "summary": f"Fixture-based analysis for {scenario.scenario_id}",
            "confidence": 75,
            "recommended_action": "investigate",
            "playbook": [],
            "tools_called": list(evidence.keys()),
        }

    def run_all(self, scenarios_root: str) -> ScoreCard:
        scenarios = self._loader.load_all(scenarios_root)
        scores = []
        for scenario, expected, alert, evidence in scenarios:
            score = self.run_scenario_with_fixture(scenario, expected, alert, evidence)
            scores.append(score)
        return self._build_scorecard(scores)

    def _build_scorecard(self, scores: list[RCAScore]) -> ScoreCard:
        passed = sum(1 for s in scores if s.passed)
        composites = [s.composite for s in scores]
        mean_comp = sum(composites) / max(len(composites), 1)
        return ScoreCard(
            run_id=str(uuid.uuid4())[:8],
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_scenarios=len(scores),
            passed=passed,
            failed=len(scores) - passed,
            pass_rate=passed / max(len(scores), 1),
            mean_composite=round(mean_comp, 4),
            scores=scores,
        )
