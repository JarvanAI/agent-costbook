import json
from decimal import Decimal
from pathlib import Path

from agent_costbook.estimates import evaluate_candidate
from agent_costbook.models import EstimateIn
from agent_costbook.offline import estimate_snapshot

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

PLAN = {
    "snapshot_id": "snap_plan",
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
    "rates": {
        "uncached_input_per_million": "2",
        "cache_read_per_million": "0.5",
        "cache_write_per_million": "3",
        "billed_output_per_million": "8",
    },
    "subscription": {
        "monthly_price": "20",
        "price_period": "month",
        "quota_multiplier": "2",
        "baseline_tasks": "100",
        "utilization": "0.5",
        "weight": "1",
        "task_profile": "coding",
    },
}

USAGE = {
    "uncached_input": "1000",
    "cache_read": "2000",
    "cache_write": "500",
    "billed_output": "400",
}


def _run(method, **overrides):
    record = overrides.pop("record", PLAN)
    payload = {
        "candidate_id": "c1",
        "method": method,
        "usage": USAGE,
        "extra_cost": "0.01",
        "currency": "USD",
        "private_rates": None,
        "record": record,
        "conflict": False,
    }
    payload.update(overrides)
    return evaluate_candidate(**payload)


def _record(**subscription):
    record = dict(PLAN)
    record["subscription"] = {**PLAN["subscription"], **subscription}
    return record


def _proxy_is_not_a_success_rate(result: dict) -> None:
    encoded = json.dumps(result, allow_nan=False)
    assert "Infinity" not in encoded
    assert "NaN" not in encoded
    proxy = result["quality_proxy"]
    assert proxy["explains"] == "not_a_measured_success_rate"
    assert "success_rate" not in result["metrics"]
    assert "do_not_reweight_for_routing" in result["assumptions"]


def test_m3_with_weight_one_matches_m2_and_does_not_claim_success():
    baseline = _run("M2")
    explicit = _run("M3")
    omitted = _run("M3", record=_record(weight=None))
    assert baseline["status"] == "ok"
    assert explicit["status"] == "ok"
    assert explicit["formula_version"] == "m3-v1"
    assert Decimal(explicit["metrics"]["K"]) == Decimal(baseline["metrics"]["K"])
    assert Decimal(explicit["metrics"]["N"]) == Decimal(baseline["metrics"]["N"])
    assert Decimal(omitted["metrics"]["K"]) == Decimal(baseline["metrics"]["K"])
    assert omitted["quality_proxy"]["kind"] == "disabled"
    assert explicit["quality_proxy"]["kind"] == "linear_index"
    _proxy_is_not_a_success_rate(explicit)
    _proxy_is_not_a_success_rate(omitted)


def test_m3_divides_m2_by_the_capability_weight():
    weighted = _run("M3", record=_record(weight="2"))
    assert Decimal(weighted["metrics"]["N"]) == Decimal("100")
    assert Decimal(weighted["metrics"]["K"]) == Decimal("0.1")
    assert weighted["quality_proxy"]["weight"] == "2"


def test_m3_private_price_period_and_currency_stay_explicit():
    private = _run("M3", private_subscription={"monthly_price": "10"})
    assert Decimal(private["metrics"]["K"]) == Decimal("0.1")
    assert private["subscription_provenance"]["monthly_price"] == "request_override"
    assert "10" not in " ".join(private["assumptions"])
    yearly = _run("M3", record=_record(price_period="year"))
    assert yearly["status"] == "invalid_input"
    assert yearly["metrics"] is None
    foreign = _run("M3", currency="CNY")
    assert foreign["status"] == "invalid_input"
    assert "currency_conversion_refused" in foreign["assumptions"]


def test_m5_with_weight_one_matches_m4_cost():
    baseline = _run("M4")
    explicit = _run("M5")
    omitted = _run("M5", record=_record(weight=None))
    assert Decimal(baseline["metrics"]["cost"]) == Decimal("0.0177")
    assert explicit["formula_version"] == "m5-v1"
    assert Decimal(explicit["metrics"]["cost"]) == Decimal(baseline["metrics"]["cost"])
    assert Decimal(explicit["metrics"]["K"]) == Decimal(baseline["metrics"]["cost"])
    assert Decimal(omitted["metrics"]["K"]) == Decimal(baseline["metrics"]["cost"])
    assert omitted["quality_proxy"]["kind"] == "disabled"
    _proxy_is_not_a_success_rate(explicit)
    halved = _run("M5", record=_record(weight="2"))
    assert Decimal(halved["metrics"]["K"]) == Decimal("0.00885")
    assert Decimal(halved["metrics"]["cost"]) == Decimal("0.0177")


def test_m5_private_rate_changes_cost_and_currency_is_not_converted():
    overridden = _run("M5", private_rates={"uncached_input_per_million": "1"})
    assert Decimal(overridden["metrics"]["cost"]) == Decimal("0.0167")
    assert Decimal(overridden["metrics"]["K"]) == Decimal("0.0167")
    assert overridden["rate_provenance"]["uncached_input_per_million"] == "request_override"
    foreign = _run("M5", currency="EUR")
    assert foreign["status"] == "invalid_input"
    assert foreign["metrics"] is None


def test_unknown_method_stays_unsupported():
    result = _run("M9")
    assert result["status"] == "unsupported_method"
    assert result["metrics"] is None
    assert result["formula_version"] is None


def test_v1_snapshot_keeps_m4_and_rejects_methods_added_later():
    raw = (FIXTURES / "ac-v0.2-published-1.json").read_bytes()
    document = json.loads(raw)
    request = json.loads((ROOT / "examples" / "estimate-request.json").read_text())
    original = EstimateIn.model_validate(request)
    kept = estimate_snapshot(document, original)
    assert kept["formula_version"] == "ac-formulas-v1"
    assert Decimal(kept["results"][0]["metrics"]["cost"]) == Decimal("0.0177")
    for method in ("M3", "M5", "M7"):
        request["method"] = method
        estimated = estimate_snapshot(document, EstimateIn.model_validate(request))
        assert estimated["formula_version"] == "ac-formulas-v1"
        result = estimated["results"][0]
        assert result["status"] == "unsupported_method"
        assert result["metrics"] is None
        assert result["formula_version"] is None
        assert "0.0177" not in json.dumps(result)
    current = dict(document)
    current["formula_version"] = "ac-formulas-v2"
    request["method"] = "M5"
    upgraded = estimate_snapshot(current, EstimateIn.model_validate(request))
    assert upgraded["formula_version"] == "ac-formulas-v2"
    assert Decimal(upgraded["results"][0]["metrics"]["K"]) == Decimal("0.0177")
    assert json.loads(raw) == document
    assert (FIXTURES / "ac-v0.2-published-1.json").read_bytes() == raw
    with_bad = dict(document)
    with_bad["formula_version"] = "ac-formulas-v0"
    try:
        estimate_snapshot(with_bad, original)
    except Exception as exc:
        assert getattr(exc, "code", None) == "unsupported_formula"
    else:
        raise AssertionError("old formula set was accepted")
