from dataclasses import dataclass
from sentinelbench.schema import ScoreCard


@dataclass
class DiffReport:
    run_a_id: str
    run_b_id: str
    composite_delta: float
    pass_rate_delta: float
    regressions: list[str]
    improvements: list[str]
    unchanged: list[str]


class BaselineComparator:
    def compare(self, run_a: ScoreCard, run_b: ScoreCard) -> DiffReport:
        a_by_id = {s.scenario_id: s for s in run_a.scores}
        b_by_id = {s.scenario_id: s for s in run_b.scores}
        regressions, improvements, unchanged = [], [], []
        for sid, b_score in b_by_id.items():
            a_score = a_by_id.get(sid)
            if a_score is None:
                improvements.append(sid)
                continue
            delta = b_score.composite - a_score.composite
            if delta < -0.05:
                regressions.append(sid)
            elif delta > 0.05:
                improvements.append(sid)
            else:
                unchanged.append(sid)
        return DiffReport(
            run_a_id=run_a.run_id,
            run_b_id=run_b.run_id,
            composite_delta=round(run_b.mean_composite - run_a.mean_composite, 4),
            pass_rate_delta=round(run_b.pass_rate - run_a.pass_rate, 4),
            regressions=regressions,
            improvements=improvements,
            unchanged=unchanged,
        )
