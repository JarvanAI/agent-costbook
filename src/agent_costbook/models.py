from __future__ import annotations

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


class EstimateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(min_length=1, max_length=16)
    snapshot_id: str | None = None
    currency: str | None = Field(default=None, max_length=12)
    usage: dict[str, str] | None = None
    extra_cost: str | None = None
    candidates: list[CandidateIn] = Field(min_length=1)

    @field_validator("usage")
    @classmethod
    def usage_values_are_strings(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if any(not isinstance(item, str) for item in value.values()):
            raise ValueError("usage values must be decimal strings")
        return value
