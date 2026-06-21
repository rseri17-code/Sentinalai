from pydantic import BaseModel, Field, model_validator


class Scenario(BaseModel):
    schema_version: str
    scenario_id: str
    title: str
    failure_mode: str
    severity: str
    affected_service: str
    difficulty: str
    available_evidence: list[str]
    tags: list[str] = []


class ExpectedAnswer(BaseModel):
    schema_version: str
    root_cause_category: str
    required_keywords: list[str]
    forbidden_keywords: list[str] = []
    required_evidence_sources: list[str]
    optimal_trajectory: list[str] = []
    max_investigation_loops: int = 15
    confidence_floor: float = 0.60

    @model_validator(mode="after")
    def validate_keywords_disjoint(self) -> "ExpectedAnswer":
        overlap = set(self.required_keywords) & set(self.forbidden_keywords)
        if overlap:
            raise ValueError(f"required_keywords and forbidden_keywords overlap: {overlap}")
        return self


class RCAScore(BaseModel):
    scenario_id: str
    root_cause_correctness: float
    evidence_completeness: float
    tool_grounding: float
    red_herring_avoidance: float
    timeline_quality: float
    confidence_calibration: float
    action_quality: float
    composite: float
    passed: bool
    details: dict = {}


class ScoreCard(BaseModel):
    run_id: str
    timestamp: str
    total_scenarios: int
    passed: int
    failed: int
    pass_rate: float
    mean_composite: float
    scores: list[RCAScore]
    by_difficulty: dict[str, float] = {}
    by_failure_mode: dict[str, float] = {}
