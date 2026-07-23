from dataclasses import dataclass, field


@dataclass
class TestCase:
    id: str
    bug_report: str
    expected_severity: str  # critical | high | medium | low
    expected_category: str  # frontend | backend | infra | data | other
    notes: str = ""  # why this case is here / what it's meant to catch


@dataclass
class ModelOutput:
    test_id: str
    raw_text: str
    predicted_severity: str | None
    predicted_category: str | None
    cost_usd: float
    duration_ms: int
    parse_error: str = ""


@dataclass
class ScoredResult:
    test_id: str
    predicted_severity: str | None
    predicted_category: str | None
    severity_correct: bool
    category_correct: bool
    judge_score: float  # 0.0-1.0, reasoning quality
    judge_rationale: str
    cost_usd: float
    duration_ms: int

    @property
    def fully_correct(self) -> bool:
        return self.severity_correct and self.category_correct


@dataclass
class RunSummary:
    prompt_version: str
    model: str
    total_cases: int
    severity_accuracy: float
    category_accuracy: float
    fully_correct_rate: float
    avg_judge_score: float
    total_cost_usd: float
    results: list[ScoredResult] = field(default_factory=list)
    provider: str = "claude"
