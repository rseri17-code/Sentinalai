import re
from sentinelbench.schema import ExpectedAnswer, RCAScore

COMPOSITE_WEIGHTS = {
    "root_cause_correctness": 0.35,
    "evidence_completeness": 0.20,
    "tool_grounding": 0.15,
    "red_herring_avoidance": 0.15,
    "timeline_quality": 0.10,
    "confidence_calibration": 0.03,
    "action_quality": 0.02,
}
PASS_THRESHOLD = 0.60

_ENTITY_RE = re.compile(r'\b([a-z][a-z0-9]{5,}(?:[_-][a-z0-9]+)+)\b')
_ACTION_KEYWORDS = [
    "investigate", "restart", "rollback", "scale", "alert",
    "escalate", "patch", "rotate", "drain", "flush",
]


class RCAScorer:
    def score(
        self,
        scenario_id: str,
        result: dict,
        expected: ExpectedAnswer,
        evidence_used: list[str] = [],
    ) -> RCAScore:
        rca_text = result.get("root_cause", "") + " " + result.get("summary", "")
        rca_lower = rca_text.lower()

        rcc = self._root_cause_correctness(rca_lower, expected)
        ec = self._evidence_completeness(expected, set(evidence_used))
        tg = self._tool_grounding(rca_text, result, evidence_used)
        rha = self._red_herring_avoidance(rca_lower, expected)
        tq = self._timeline_quality(result, expected)
        cc = self._confidence_calibration(result, expected)
        aq = self._action_quality(result)

        dims = {
            "root_cause_correctness": rcc,
            "evidence_completeness": ec,
            "tool_grounding": tg,
            "red_herring_avoidance": rha,
            "timeline_quality": tq,
            "confidence_calibration": cc,
            "action_quality": aq,
        }
        composite = sum(dims[k] * COMPOSITE_WEIGHTS[k] for k in dims)
        composite = round(composite, 4)

        return RCAScore(
            scenario_id=scenario_id,
            root_cause_correctness=rcc,
            evidence_completeness=ec,
            tool_grounding=tg,
            red_herring_avoidance=rha,
            timeline_quality=tq,
            confidence_calibration=cc,
            action_quality=aq,
            composite=composite,
            passed=composite >= PASS_THRESHOLD,
        )

    def _root_cause_correctness(self, rca_lower: str, expected: ExpectedAnswer) -> float:
        score = 0.0
        if expected.root_cause_category.lower() in rca_lower:
            score += 0.5
        if expected.required_keywords:
            kw_hits = sum(1 for kw in expected.required_keywords if kw.lower() in rca_lower)
            score += 0.5 * (kw_hits / len(expected.required_keywords))
        return round(score, 4)

    def _evidence_completeness(self, expected: ExpectedAnswer, actual_called: set) -> float:
        required = set(expected.required_evidence_sources)
        if not required:
            return 1.0
        return round(len(required & actual_called) / len(required), 4)

    def _tool_grounding(self, rca_text: str, result: dict, evidence_used: list[str]) -> float:
        entities = _ENTITY_RE.findall(rca_text)
        if not entities:
            return 0.8
        evidence_text = " ".join(str(e) for e in evidence_used).lower()
        found = sum(1 for e in entities if e.lower() in evidence_text)
        return round(found / len(entities), 4)

    def _red_herring_avoidance(self, rca_lower: str, expected: ExpectedAnswer) -> float:
        for kw in expected.forbidden_keywords:
            if kw.lower() in rca_lower:
                return 0.0
        return 1.0

    def _timeline_quality(self, result: dict, expected: ExpectedAnswer) -> float:
        if not expected.optimal_trajectory:
            return 0.8
        steps = set(result.get("playbook", []) + result.get("tools_called", []))
        hits = sum(1 for step in expected.optimal_trajectory if step in steps)
        return round(hits / len(expected.optimal_trajectory), 4)

    def _confidence_calibration(self, result: dict, expected: ExpectedAnswer) -> float:
        raw = result.get("confidence", 50)
        normalized = raw / 100.0
        abs_diff = abs(normalized - expected.confidence_floor)
        return round(max(0.0, 1.0 - 2 * abs_diff), 4)

    def _action_quality(self, result: dict) -> float:
        action = result.get("recommended_action", "")
        if not action:
            return 0.0
        has_keyword = any(kw in action.lower() for kw in _ACTION_KEYWORDS)
        return 1.0 if has_keyword else 0.5
