from decimal import Decimal

from agent_costbook.estimates import (
    apply_reference_comparison,
    apply_scenario,
    evaluate_candidate,
)

PUBLISHED = {
    "snapshot_id": "snap_synthetic",
    "published_at": "2026-09-25T00:00:00+00:00",
    "provider": "example",
    "channel": "subscription",
    "model": "example-model",
    "effort": "high",
    "plan": "plus",
    "feature_scope": "code",
    "window_start": "2026-09-01",
    "window_end": "2026-10-01",
    "currency": "USD",
    "evidence_ids": ["ev_plan"],
    "rates": {},
    "subscription": {
        "monthly_price": "20",
        "price_period": "month",
        "quota_multiplier": "2",
        "baseline_tasks": "100",
        "baseline_api_budget": "100",
        "utilization": "0.5",
        "cost_per_task": "0.5",
        "weight": "1",
        "task_profile": "coding",
    },
}


def _run(method, **overrides):
    record = overrides.pop("record", PUBLISHED)
    payload = {
        "candidate_id": overrides.pop("candidate_id", "c1"),
        "method": method,
        "usage": None,
        "extra_cost": None,
        "currency": "USD",
        "private_rates": None,
        "record": record,
        "conflict": False,
    }
    payload.update(overrides)
    return evaluate_candidate(**payload)


def _record(**subscription):
    record = dict(PUBLISHED)
    record["subscription"] = {**PUBLISHED["subscription"], **subscription}
    return record


def _context(candidate_id, **overrides):
    context = {
        "candidate_id": candidate_id,
        "provider": "example",
        "feature_scope": "code",
        "window_start": "2026-09-01",
        "window_end": "2026-10-01",
        "currency": "USD",
        "price_period": "month",
        "model": "example-model",
        "effort": "high",
        "task_profile": "coding",
        "baseline_group": "",
        "has_own_budget": True,
        "has_own_tasks": True,
    }
    context.update(overrides)
    return context


def test_m1_quota_price_is_price_over_multiplier():
    record = _record(monthly_price="100", quota_multiplier="5")
    result = _run("M1", record=record)
    assert result["status"] == "ok"
    assert result["formula_version"] == "m1-v1"
    assert Decimal(result["metrics"]["K"]) == Decimal("20")
    assert Decimal(result["metrics"]["amortization"]) == Decimal("20")
    assert "total" not in result["metrics"]
    assert result["metrics"]["cash_increment"] is None
    assert result["metrics"]["quota_consumption"] is None
    assert "cash_increment" in result["missing_fields"]
    assert "quota_consumption" in result["missing_fields"]
    assert "quota_metric" not in result["metrics"]


def test_m1_same_group_can_compare_and_other_groups_cannot():
    heavy = _run(
        "M1",
        candidate_id="heavy",
        record=_record(monthly_price="100", quota_multiplier="5"),
    )
    plus = _run(
        "M1",
        candidate_id="plus",
        record=_record(monthly_price="20", quota_multiplier="1", plan="base"),
    )
    image = _run(
        "M1",
        candidate_id="image",
        record=_record(monthly_price="20", quota_multiplier="1"),
    )
    same = apply_reference_comparison(
        "M1",
        [heavy, plus],
        [
            _context("heavy", baseline_group="plus-quota"),
            _context("plus", baseline_group="plus-quota"),
        ],
        reference_candidate_id="plus",
    )
    assert Decimal(same[0]["metrics"]["cost_ratio"]) == Decimal("1")
    assert "rank" in same[0]
    assert "cost_ratio" not in same[1]["metrics"]
    mixed = apply_reference_comparison(
        "M1",
        [heavy, image],
        [
            _context("heavy", baseline_group="plus-quota"),
            _context("image", baseline_group="plus-quota", feature_scope="image"),
        ],
        reference_candidate_id="heavy",
    )
    assert "cost_ratio" not in mixed[1]["metrics"]
    assert "rank" not in mixed[0]
    assert "rank" not in mixed[1]
    assert "not_comparable" in mixed[1]["assumptions"]
    ungrouped = apply_reference_comparison(
        "M1",
        [heavy, plus],
        [_context("heavy"), _context("plus", provider="example")],
        reference_candidate_id="plus",
    )
    assert "cost_ratio" not in ungrouped[0]["metrics"]
    assert "rank" not in ungrouped[0]
    assert "not_comparable" in ungrouped[0]["assumptions"]
    cross_provider = apply_reference_comparison(
        "M1",
        [heavy, plus],
        [
            _context("heavy", provider="openai", baseline_group="plus-quota"),
            _context("plus", provider="xai", baseline_group="plus-quota"),
        ],
        reference_candidate_id="plus",
    )
    assert "cost_ratio" not in cross_provider[0]["metrics"]
    assert "rank" not in cross_provider[0]
    assert "not_comparable" in cross_provider[0]["assumptions"]


def test_m2_amortizes_assumed_tasks_and_does_not_rescale_measured_n():
    assumed = _run("M2")
    assert assumed["status"] == "ok"
    assert assumed["formula_version"] == "m2-v1"
    assert Decimal(assumed["metrics"]["N"]) == Decimal("100")
    assert Decimal(assumed["metrics"]["K"]) == Decimal("0.2")
    assert Decimal(assumed["metrics"]["amortization"]) == Decimal("0.2")
    measured = _run("M2", record=_record(measured_tasks="80", utilization="0.5"))
    assert Decimal(measured["metrics"]["N"]) == Decimal("80")
    assert Decimal(measured["metrics"]["K"]) == Decimal("0.25")
    assert "measured_n_not_rescaled_by_utilization" in measured["assumptions"]


def test_m6_budget_amortization_and_reference_ratio():
    candidate = _run("M6", candidate_id="higher")
    reference = _run("M6", candidate_id="base", record=_record(quota_multiplier="1"))
    assert candidate["status"] == "ok"
    assert candidate["formula_version"] == "m6-v1"
    assert Decimal(candidate["metrics"]["N"]) == Decimal("200")
    assert Decimal(candidate["metrics"]["K"]) == Decimal("0.1")
    assert Decimal(candidate["metrics"]["api_equivalent"]) == Decimal("0.5")
    assert candidate["metrics"]["quota_consumption"] is None
    assert "quota_metric" not in candidate["metrics"]
    compared = apply_reference_comparison(
        "M6",
        [candidate, reference],
        [_context("higher"), _context("base")],
        reference_candidate_id="base",
    )
    assert Decimal(compared[0]["metrics"]["cost_ratio"]) == Decimal("0.5")
    weighted = _run("M6", record=_record(weight="2"))
    assert Decimal(weighted["metrics"]["N"]) == Decimal("200")
    assert Decimal(weighted["metrics"]["K"]) == Decimal("0.05")


def test_missing_budget_is_not_one_and_is_not_ranked():
    missing = _run("M6", candidate_id="missing", record=_record(baseline_api_budget=None))
    present = _run("M6", candidate_id="present")
    assert missing["status"] == "missing_data"
    assert missing["metrics"] is None
    assert "baseline_api_budget" in missing["missing_fields"]
    assert missing["metrics"] != {"K": "10"}
    compared = apply_reference_comparison(
        "M6",
        [missing, present],
        [_context("missing"), _context("present", provider="other")],
        reference_candidate_id="present",
    )
    assert "rank" not in compared[0]
    assert "rank" not in compared[1]
    assert "cost_ratio" not in (compared[0]["metrics"] or {})


def test_explicit_equal_budget_can_cross_providers_without_becoming_the_default():
    missing = _run(
        "M6",
        candidate_id="missing",
        record=_record(baseline_api_budget=None),
        subscription_override={"baseline_api_budget": "100"},
        scenario_notes=["assumed_equal_baseline_budget"],
    )
    reference = _run("M6", candidate_id="ref", record=_record(quota_multiplier="1"))
    assert missing["status"] == "ok"
    assert "assumed_equal_baseline_budget" in missing["assumptions"]
    compared = apply_reference_comparison(
        "M6",
        [missing, reference],
        [
            _context("missing", provider="grok", has_own_budget=False),
            _context("ref", provider="openai", has_own_budget=True),
        ],
        reference_candidate_id="ref",
        allow_cross_provider=True,
    )
    assert Decimal(compared[0]["metrics"]["cost_ratio"]) == Decimal("0.5")
    refused = apply_reference_comparison(
        "M6",
        [missing, reference],
        [
            _context("missing", provider="grok", has_own_budget=False),
            _context("ref", provider="openai", has_own_budget=True),
        ],
        reference_candidate_id="ref",
    )
    assert "cost_ratio" not in refused[0]["metrics"]
    assert "rank" not in refused[0]


def test_zero_capacity_is_not_free_and_explicit_zero_price_is():
    idle = _run("M6", record=_record(utilization="0"))
    assert idle["status"] == "no_capacity"
    assert idle["metrics"]["N"] == "0"
    assert idle["metrics"]["K"] is None
    assert idle["metrics"]["amortization"] is None
    idle_free = _run("M6", record=_record(monthly_price="0", utilization="0"))
    assert idle_free["status"] == "no_capacity"
    assert idle_free["metrics"]["K"] is None
    assert "explicit_zero_price" not in idle_free["assumptions"]
    free = _run("M2", record=_record(monthly_price="0"))
    assert free["status"] == "ok"
    assert free["metrics"]["K"] == "0"
    assert "explicit_zero_price" in free["assumptions"]
    priced = _run("M6")
    cashless = dict(priced)
    assert "cash_increment" in cashless["missing_fields"]
    paid = _run("M6", marginal_cash="0")
    assert paid["metrics"]["cash_increment"] == "0"
    assert Decimal(paid["metrics"]["amortization"]) == Decimal("0.1")
    assert "total" not in paid["metrics"]
    assert "request_marginal_cash" in paid["assumptions"]
    assert "0" not in paid["assumptions"]


def test_invalid_subscription_bounds_and_reference_zero_block_only_the_ratio():
    assert _run("M6", record=_record(cost_per_task="0"))["status"] == "invalid_input"
    assert _run("M6", record=_record(weight="0"))["status"] == "invalid_input"
    assert _run("M6", record=_record(utilization="1.1"))["status"] == "invalid_input"
    assert _run("M1", record=_record(quota_multiplier=None))["status"] == "missing_data"
    free_ref = _run("M6", candidate_id="free", record=_record(monthly_price="0", quota_multiplier="1"))
    priced = _run("M6", candidate_id="priced")
    compared = apply_reference_comparison(
        "M6",
        [priced, free_ref],
        [_context("priced"), _context("free")],
        reference_candidate_id="free",
    )
    assert compared[0]["status"] == "ok"
    assert "cost_ratio" not in compared[0]["metrics"]
    assert "reference_ratio_unavailable" in compared[0]["assumptions"]


def test_mixed_currency_and_task_profile_are_not_compared():
    left = _run("M2", candidate_id="usd")
    right = _run("M2", candidate_id="cny", record=_record(), currency="CNY")
    # currency mismatch against the record is invalid before comparison
    assert right["status"] == "invalid_input"
    profiled = _run("M2", candidate_id="other")
    compared = apply_reference_comparison(
        "M2",
        [left, profiled],
        [_context("usd"), _context("other", task_profile="chat")],
        reference_candidate_id="usd",
    )
    assert "cost_ratio" not in compared[1]["metrics"]
    assert "rank" not in compared[0]
    assert "not_comparable" in compared[1]["assumptions"]


def test_equal_budget_scenario_is_explicit_and_does_not_change_the_reference():
    candidate = {
        "id": "candidate",
        "currency": "USD",
        "feature_scope": "code",
        "subscription": {
            "quota_multiplier": "2",
            "price_period": "month",
            "task_profile": "coding",
        },
    }
    reference = {
        "id": "reference",
        "currency": "USD",
        "feature_scope": "code",
        "subscription": {
            "baseline_api_budget": "100",
            "baseline_tasks": "40",
            "price_period": "month",
            "task_profile": "coding",
        },
    }
    filled, notes = apply_scenario(
        candidate,
        reference,
        {"equal_baseline_budget": True, "equal_baseline_tasks": False},
    )
    assert filled is not None
    assert filled["baseline_api_budget"] == "100"
    assert "baseline_tasks" not in filled
    assert notes == ["assumed_equal_baseline_budget"]
    untouched, empty = apply_scenario(candidate, reference, None)
    assert untouched is not None
    assert "baseline_api_budget" not in untouched
    assert empty == []
    assert "baseline_api_budget" not in candidate["subscription"]
    other_currency = dict(candidate)
    other_currency["currency"] = "CNY"
    refused, refused_notes = apply_scenario(
        other_currency,
        reference,
        {"equal_baseline_budget": True},
    )
    assert refused is not None
    assert "baseline_api_budget" not in refused
    assert refused_notes == []


def test_subscription_methods_do_not_require_token_usage():
    result = _run("M1", record=_record(monthly_price="100", quota_multiplier="5"))
    assert result["status"] == "ok"
    assert "usage" not in result["missing_fields"]


def test_different_models_compare_when_task_profile_and_own_budgets_match():
    grok = _run("M6", candidate_id="grok-high")
    astra = _run(
        "M6",
        candidate_id="astra-low",
        record=_record(quota_multiplier="1", baseline_api_budget="80"),
    )
    compared = apply_reference_comparison(
        "M6",
        [grok, astra],
        [
            _context("grok-high", provider="xai", model="grok", effort="high"),
            _context(
                "astra-low",
                provider="openai",
                model="astra",
                effort="low",
                has_own_budget=True,
            ),
        ],
        reference_candidate_id="astra-low",
    )
    assert compared[0]["status"] == "ok"
    assert "cost_ratio" in compared[0]["metrics"]
    assert "not_comparable" not in compared[0]["assumptions"]
    blank = apply_reference_comparison(
        "M6",
        [grok, astra],
        [
            _context("grok-high", task_profile=""),
            _context("astra-low", task_profile=""),
        ],
        reference_candidate_id="astra-low",
    )
    assert "cost_ratio" not in blank[0]["metrics"]
    assert "not_comparable" in blank[0]["assumptions"]


def test_omitted_weight_defaults_to_one_without_writing_the_record():
    record = _record(weight=None)
    result = _run("M6", record=record)
    assert result["status"] == "ok"
    assert Decimal(result["metrics"]["K"]) == Decimal("0.1")
    assert Decimal(result["metrics"]["N"]) == Decimal("200")
    assert "default_weight_one" in result["assumptions"]
    assert record["subscription"].get("weight") is None


def test_non_month_price_period_is_not_treated_as_monthly():
    result = _run("M6", record=_record(price_period="year"))
    assert result["status"] == "invalid_input"
    assert result["metrics"] is None
    assert "price_period_not_monthly" in result["assumptions"]


def test_m0_returns_known_plan_fields_instead_of_an_empty_object():
    result = _run("M0")
    assert result["status"] == "ok"
    assert result["metrics"]["monthly_price"] == "20"
    assert result["metrics"]["baseline_api_budget"] == "100"
    assert result["metrics"] != {}
    assert "quota_metric" not in result["metrics"]


def test_private_subscription_price_changes_only_this_request():
    secret = "10"
    result = _run("M6", private_subscription={"monthly_price": secret})
    assert result["status"] == "ok"
    assert Decimal(result["metrics"]["K"]) == Decimal("0.05")
    assert result["subscription_provenance"]["monthly_price"] == "request_override"
    assert secret not in " ".join(result["assumptions"])
    assert PUBLISHED["subscription"]["monthly_price"] == "20"
