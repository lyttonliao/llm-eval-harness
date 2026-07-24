from dataclasses import dataclass, field


@dataclass
class TestCase:
    id: str
    input: str
    expected: dict  # suite-specific: e.g. {"severity": ..., "category": ...}, {"test_code": ...}, or
    # {"must_include": [[...]], "must_exclude": [...], "max_words": ...} - values aren't always str
    notes: str = ""  # why this case is here / what it's meant to catch


@dataclass
class ModelOutput:
    test_id: str
    raw_text: str
    predicted: dict[str, str]  # full parsed model JSON, suite-specific keys (includes "reasoning")
    cost_usd: float
    duration_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)
    parse_error: str = ""


@dataclass
class ScoredResult:
    test_id: str
    predicted: dict[str, str]
    checks: dict[str, bool]  # suite-specific: e.g. {"severity": True, "category": True} or {"tests_passed": True}
    judge_score: float  # 0.0-1.0, reasoning quality
    judge_rationale: str
    cost_usd: float
    duration_ms: int
    token_usage: dict[str, int] = field(default_factory=dict)

    @property
    def fully_correct(self) -> bool:
        return all(self.checks.values())


@dataclass
class RunSummary:
    prompt_version: str
    model: str
    total_cases: int
    check_accuracies: dict[str, float]
    fully_correct_rate: float
    avg_judge_score: float
    total_cost_usd: float
    total_tokens: int = 0
    results: list[ScoredResult] = field(default_factory=list)
    provider: str = "claude"
