from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_costbook.estimates import RATE_KEYS, parse_decimal

MAX_DOCUMENT_CHARS = 256 * 1024


class EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: str = Field(min_length=1, max_length=80)
    source_url: str | None = Field(default=None, max_length=2000)
    collector_kind: str = Field(min_length=1, max_length=80)
    collector_name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)
    retrieved_at: str = Field(min_length=1, max_length=64)


class ResearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)


class RateCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uncached_input_per_million: str | None = None
    cache_read_per_million: str | None = None
    cache_write_per_million: str | None = None
    billed_output_per_million: str | None = None

    @model_validator(mode="after")
    def rates_are_finite(self) -> RateCard:
        for key in RATE_KEYS:
            value = getattr(self, key)
            if value is None:
                continue
            number = parse_decimal(value)
            if number is None or number < 0:
                raise ValueError(f"{key} must be a finite non-negative decimal string")
        return self


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_price: str | None = None
    price_period: str | None = Field(default=None, max_length=32)
    quota_multiplier: str | None = None
    baseline_tasks: str | None = None
    measured_tasks: str | None = None
    baseline_api_budget: str | None = None
    utilization: str | None = None
    cost_per_task: str | None = None
    weight: str | None = None
    task_profile: str | None = Field(default=None, max_length=120)
    baseline_group: str | None = Field(default=None, max_length=120)
    assumptions: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def amounts_are_in_range(self) -> SubscriptionIn:
        bounds = {
            "monthly_price": (Decimal("0"), None, False),
            "quota_multiplier": (Decimal("0"), None, True),
            "baseline_tasks": (Decimal("0"), None, False),
            "measured_tasks": (Decimal("0"), None, False),
            "baseline_api_budget": (Decimal("0"), None, True),
            "utilization": (Decimal("0"), Decimal("1"), False),
            "cost_per_task": (Decimal("0"), None, True),
            "weight": (Decimal("0"), None, True),
        }
        for key, (low, high, greater) in bounds.items():
            raw = getattr(self, key)
            if raw is None:
                continue
            number = parse_decimal(raw)
            if number is None:
                raise ValueError(f"{key} must be a finite decimal string")
            if greater and number <= low or not greater and number < low:
                raise ValueError(f"{key} is outside its allowed range")
            if high is not None and number > high:
                raise ValueError(f"{key} is outside its allowed range")
        return self


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="", max_length=80)
    plan: str = Field(min_length=1, max_length=80)
    feature_scope: str = Field(min_length=1, max_length=80)
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)
    currency: str = Field(min_length=1, max_length=12)
    rates: RateCard
    evidence_indexes: list[int] = Field(min_length=1)
    base_snapshot_id: str | None = Field(default=None, max_length=80)
    subscription: SubscriptionIn | None = None


class ContributionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    research: ResearchIn
    evidence: list[EvidenceIn] = Field(min_length=1)
    records: list[RecordIn] = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_indexes_exist(self) -> ContributionIn:
        last = len(self.evidence) - 1
        for record in self.records:
            if any(index < 0 or index > last for index in record.evidence_indexes):
                raise ValueError("evidence index is outside the submitted evidence list")
        return self


class PrivateSubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthly_price: str | None = None

    @field_validator("monthly_price")
    @classmethod
    def price_is_non_negative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        number = parse_decimal(value)
        if number is None or number < 0:
            raise ValueError("monthly_price must be a finite non-negative decimal string")
        return value


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="", max_length=80)
    plan: str = Field(min_length=1, max_length=80)
    feature_scope: str = Field(min_length=1, max_length=80)
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)
    private_rates: RateCard | None = None
    private_subscription: PrivateSubscriptionIn | None = None
    marginal_cash: str | None = None
    agent_id: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("marginal_cash")
    @classmethod
    def marginal_cash_is_non_negative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        number = parse_decimal(value)
        if number is None or number < 0:
            raise ValueError("marginal_cash must be a finite non-negative decimal string")
        return value


class ScenarioIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    equal_baseline_budget: bool = False
    equal_baseline_tasks: bool = False


class AttemptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cash: str
    api_equivalent: str | None = None
    succeeded: bool

    @model_validator(mode="after")
    def amounts_are_non_negative(self) -> AttemptIn:
        for key in ("cash", "api_equivalent"):
            raw = getattr(self, key)
            if raw is None:
                continue
            number = parse_decimal(raw)
            if number is None or number < 0:
                raise ValueError(f"{key} must be a finite non-negative decimal string")
        return self


class ObservedTaskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=160)
    attempts: list[AttemptIn] = Field(min_length=1)


class ObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=120)
    channel: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    effort: str = Field(default="", max_length=80)
    plan: str = Field(min_length=1, max_length=80)
    feature_scope: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=1, max_length=12)
    period_start: str = Field(min_length=1, max_length=64)
    period_end: str = Field(min_length=1, max_length=64)
    task_category: str = Field(min_length=1, max_length=120)
    acceptance: str = Field(min_length=1, max_length=120)
    subscription_cash: str
    tasks: list[ObservedTaskIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def cash_and_period_are_explicit(self) -> ObservationIn:
        number = parse_decimal(self.subscription_cash)
        if number is None or number < 0:
            raise ValueError("subscription_cash must be a finite non-negative decimal string")
        from datetime import datetime

        try:
            start = datetime.fromisoformat(self.period_start)
            end = datetime.fromisoformat(self.period_end)
        except ValueError as exc:
            raise ValueError("period_start and period_end must be ISO timestamps") from exc
        if (start.tzinfo is None) != (end.tzinfo is None):
            raise ValueError(
                "period_start and period_end must both include a timezone or both omit it"
            )
        try:
            ordered = start >= end
        except TypeError as exc:
            raise ValueError(
                "period_start and period_end must both include a timezone or both omit it"
            ) from exc
        if ordered:
            raise ValueError("period_end must be after period_start")
        return self


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=16)
    snapshot_id: str | None = None
    currency: str | None = Field(default=None, max_length=12)
    usage: dict[str, str] | None = None
    extra_cost: str | None = None
    reference_candidate_id: str | None = Field(default=None, max_length=160)
    task_category: str | None = Field(default=None, max_length=120)
    acceptance: str | None = Field(default=None, max_length=120)
    scenario: ScenarioIn | None = None
    candidates: list[CandidateIn] = Field(min_length=1)

    @field_validator("usage")
    @classmethod
    def usage_values_are_strings(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError("usage values must be decimal strings")
        return value

    @model_validator(mode="after")
    def reference_matches_one_candidate(self) -> EstimateIn:
        if self.reference_candidate_id is None:
            return self
        matches = [
            candidate.candidate_id
            for candidate in self.candidates
            if candidate.candidate_id == self.reference_candidate_id
        ]
        if len(matches) != 1:
            raise ValueError("reference_candidate_id must match exactly one candidate")
        return self
