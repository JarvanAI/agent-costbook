from decimal import Decimal

from agent_costbook.estimates import evaluate_candidate

PUBLISHED = {
    "snapshot_id": "snap_synthetic",
    "published_at": "2026-09-25T00:00:00+00:00",
    "currency": "USD",
    "evidence_ids": ["ev_synthetic"],
    "rates": {
        "uncached_input_per_million": "2",
        "cache_read_per_million": "0.5",
        "cache_write_per_million": "3",
        "billed_output_per_million": "8",
    },
}

USAGE = {
    "uncached_input": "1000",
    "cache_read": "2000",
    "cache_write": "500",
    "billed_output": "400",
    "reasoning": "120",
}


def _run(**overrides):
    payload = {
        "candidate_id": "c1",
        "method": "M4",
        "usage": USAGE,
        "extra_cost": "0.01",
        "currency": "USD",
        "private_rates": None,
        "record": PUBLISHED,
        "conflict": False,
    }
    payload.update(overrides)
    return evaluate_candidate(**payload)


def test_m4_synthetic_total_is_0_0177():
    result = _run()
    assert result["status"] == "ok"
    assert result["method"] == "M4"
    assert result["formula_version"] == "m4-v1"
    assert Decimal(result["metrics"]["cost"]) == Decimal("0.0177")
    assert result["metrics"]["currency"] == "USD"
    assert result["snapshot_id"] == "snap_synthetic"
    assert result["sources"] == ["ev_synthetic"]
    assert "reasoning_tokens_included_in_billed_output" in result["assumptions"]


def test_m4_input_rate_override_is_0_0167_and_reasoning_is_not_added():
    overridden = _run(private_rates={"uncached_input_per_million": "1"})
    without_reasoning = _run(
        usage={key: value for key, value in USAGE.items() if key != "reasoning"},
        private_rates={"uncached_input_per_million": "1"},
    )
    assert Decimal(overridden["metrics"]["cost"]) == Decimal("0.0167")
    assert Decimal(without_reasoning["metrics"]["cost"]) == Decimal("0.0167")
    assert "request_private_rate_override" in overridden["assumptions"]


def test_missing_rate_stays_missing_instead_of_zero_or_one():
    rates = dict(PUBLISHED["rates"])
    rates.pop("cache_read_per_million")
    record = dict(PUBLISHED)
    record["rates"] = rates
    result = _run(record=record)
    assert result["status"] == "missing_data"
    assert result["metrics"] is None
    assert "cache_read_per_million" in result["missing_fields"]
    assert result["metrics"] != {"cost": "0", "currency": "USD"}


def test_omitted_extra_cost_is_not_treated_as_zero():
    result = _run(extra_cost=None)
    assert result["status"] == "missing_data"
    assert "extra_cost" in result["missing_fields"]


def test_negative_rate_and_non_finite_usage_are_invalid():
    negative = _run(private_rates={"billed_output_per_million": "-1"})
    infinite = _run(usage={**USAGE, "billed_output": "NaN"})
    assert negative["status"] == "invalid_input"
    assert infinite["status"] == "invalid_input"
    assert "invalid_usage" in infinite["assumptions"]


def test_overlapping_output_buckets_are_invalid_usage():
    result = _run(usage={**USAGE, "output": "400"})
    assert result["status"] == "invalid_input"
    assert "invalid_usage" in result["assumptions"]
    assert result["metrics"] is None


def test_reasoning_larger_than_billed_output_is_invalid_usage():
    result = _run(usage={**USAGE, "reasoning": "401"})
    assert result["status"] == "invalid_input"
    assert "invalid_usage" in result["assumptions"]


def test_unimplemented_methods_are_unsupported():
    for method in ("M1", "M2", "M3", "M5", "M6", "M7", "M9"):
        result = _run(method=method)
        assert result["status"] == "unsupported_method"
        assert result["metrics"] is None
        assert result["formula_version"] is None


def test_omitted_usage_with_zero_extra_cost_is_missing_not_free():
    for omitted in (None, {}):
        result = _run(usage=omitted, extra_cost="0")
        assert result["status"] == "missing_data"
        assert result["metrics"] is None
        assert "usage" in result["missing_fields"]


def test_explicit_zero_token_counts_can_cost_zero_without_missing_rate():
    result = _run(
        usage={
            "uncached_input": "0",
            "cache_read": "0",
            "cache_write": "0",
            "billed_output": "0",
        },
        extra_cost="0",
        record={
            "snapshot_id": "snap_synthetic",
            "published_at": "2026-09-25T00:00:00+00:00",
            "currency": "USD",
            "evidence_ids": ["ev_synthetic"],
            "rates": {},
        },
    )
    assert result["status"] == "ok"
    assert Decimal(result["metrics"]["cost"]) == Decimal("0")


def test_m0_units_follow_the_record_currency():
    record = dict(PUBLISHED)
    record["currency"] = "CNY"
    result = _run(method="M0", record=record, currency="CNY")
    assert result["status"] == "ok"
    assert set(result["units"].values()) == {"CNY_per_million_tokens"}


def test_invalid_usage_sets_error_code():
    result = _run(usage={**USAGE, "output": "400"})
    assert result["status"] == "invalid_input"
    assert result["error_code"] == "invalid_usage"
    assert result["metrics"] is None


def test_private_override_provenance_does_not_reveal_the_secret_rate():
    secret = "9.87654321"
    result = _run(private_rates={"uncached_input_per_million": secret})
    assert result["rate_provenance"]["uncached_input_per_million"] == "request_override"
    assert result["rate_provenance"]["billed_output_per_million"] == "public"
    assert secret not in str(result["rate_provenance"])
    assert secret not in " ".join(result["assumptions"])


def test_m0_returns_rates_without_summing_a_task_cost():
    result = _run(method="M0")
    assert result["status"] == "ok"
    assert result["formula_version"] == "m0-v1"
    assert result["metrics"]["billed_output_per_million"] == "8"
    assert "cost" not in result["metrics"]


def test_currency_mismatch_does_not_convert():
    result = _run(currency="CNY")
    assert result["status"] == "invalid_input"
    assert "currency_conversion_refused" in result["assumptions"]
    assert result["metrics"] is None


def test_openrouter_excerpt_cost_uses_only_known_rates():
    record = {
        "snapshot_id": "snap_openrouter",
        "published_at": "2026-09-25T05:54:07+00:00",
        "currency": "USD",
        "evidence_ids": ["ev_openrouter"],
        "rates": {
            "uncached_input_per_million": "0.15",
            "cache_read_per_million": "0.075",
            "billed_output_per_million": "0.6",
        },
    }
    result = evaluate_candidate(
        candidate_id="gpt-4o-mini",
        method="M4",
        usage={"uncached_input": "1000", "billed_output": "400"},
        extra_cost="0",
        currency="USD",
        private_rates=None,
        record=record,
        conflict=False,
    )
    assert Decimal(result["metrics"]["cost"]) == Decimal("0.00039")
    missing = evaluate_candidate(
        candidate_id="gpt-4o-mini",
        method="M4",
        usage={"uncached_input": "1000", "cache_write": "10", "billed_output": "400"},
        extra_cost="0",
        currency="USD",
        private_rates=None,
        record=record,
        conflict=False,
    )
    assert missing["status"] == "missing_data"
    assert "cache_write_per_million" in missing["missing_fields"]
