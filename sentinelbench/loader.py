import yaml
import json
from pathlib import Path
from sentinelbench.schema import Scenario, ExpectedAnswer


class ScenarioLoader:
    def load(self, scenario_dir: str | Path) -> tuple[Scenario, ExpectedAnswer, dict, dict]:
        d = Path(scenario_dir)
        scenario = Scenario(**yaml.safe_load((d / "scenario.yml").read_text()))
        expected = ExpectedAnswer(**yaml.safe_load((d / "answer.yml").read_text()))
        alert = json.loads((d / "alert.json").read_text())

        evidence: dict = {}
        evidence_dir = d / "evidence"
        if evidence_dir.exists():
            for f in sorted(evidence_dir.glob("*.json")):
                evidence[f.stem] = json.loads(f.read_text())

        missing = set(expected.required_evidence_sources) - set(scenario.available_evidence)
        if missing:
            raise ValueError(f"required_evidence_sources not in available_evidence: {missing}")

        return scenario, expected, alert, evidence

    def load_all(self, scenarios_root: str | Path) -> list[tuple[Scenario, ExpectedAnswer, dict, dict]]:
        root = Path(scenarios_root)
        results = []
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "scenario.yml").exists():
                results.append(self.load(d))
        return results
