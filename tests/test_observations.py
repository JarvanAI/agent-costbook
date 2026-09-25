import json
from decimal import Decimal

from agent_costbook.estimates import evaluate_candidate
from agent_costbook.observations import aggregate_observation
from agent_costbook.store import Store, StoreError


def _sample(**overrides):
    tasks = []
    for index in range(10):
        attempts = [{"cash": "0", "api_equivalent": "10", "succeeded": True}]
        if index == 0:
            attempts.append(
                {
                    "cash": "0",
                    "api_equivalent": "0",
                    "succeeded": True,
                    "note": "retry of the same task",
                }
            )
        tasks.append({"task_id": f"task-{index}", "attempts": attempts})
    payload = {
        "provider": "example",
        "channel": "subscription",
        "model": "example-model",
        "effort": "high",
        "plan": "plus",
        "feature_scope": "code",
        "currency": "CNY",
        "period_start": "2026-09-01",
        "period_end": "2026-10-01",
        "task_category": "coding",
        "acceptance": "tests_passed",
        "subscription_cash": "25",
        "tasks": tasks,
    }
    payload.update(overrides)
    return payload


def _measured(measurement, **overrides):
    payload = {
        "candidate_id": "measured",
        "method": "M7",
        "usage": None,
        "extra_cost": None,
        "currency": "CNY",
        "private_rates": None,
        "record": {"currency": "CNY", "snapshot_id": "snap-1", "evidence_ids": []},
        "conflict": False,
        "measurement": measurement,
    }
    payload.update(overrides)
    return evaluate_candidate(**payload)


def test_attributed_cash_excludes_api_equivalent_and_counts_success_once():
    measurement = aggregate_observation(_sample())
    assert Decimal(measurement["attributed_cash"]) == Decimal("25")
    assert measurement["successful_tasks"] == "10"
    assert measurement["sample_size"] == "10"
    assert measurement["attempt_count"] == "11"
    assert Decimal(measurement["api_equivalent"]) == Decimal("100")
    result = _measured(measurement)
    encoded = json.dumps(result, allow_nan=False)
    assert "Infinity" not in encoded and "NaN" not in encoded
    assert result["status"] == "ok"
    assert result["formula_version"] == "m7-v1"
    assert Decimal(result["metrics"]["K"]) == Decimal("2.5")
    assert Decimal(result["metrics"]["attributed_cash"]) == Decimal("25")
    assert result["metrics"]["api_equivalent"] == "100"
    assert "api_equivalent_excluded_from_cash" in result["assumptions"]
    assert "retries_counted_in_cash_not_in_success" in result["assumptions"]
    assert "subscription_cash_once_per_period" in result["assumptions"]
    assert result["measurement"]["sample_size"] == "10"
    assert result["measurement"]["period_start"] == "2026-09-01"
    assert result["measurement"]["task_category"] == "coding"
    assert result["measurement"]["acceptance"] == "tests_passed"
    assert "task-0" not in encoded
    assert "retry of the same task" not in encoded


def test_retry_cash_is_kept_and_a_second_success_is_not():
    payload = _sample(
        subscription_cash="20",
        tasks=[
            {
                "task_id": "same",
                "attempts": [
                    {"cash": "3", "api_equivalent": "40", "succeeded": False},
                    {"cash": "2", "api_equivalent": "60", "succeeded": True},
                ],
            },
            {
                "task_id": "same",
                "attempts": [{"cash": "0", "api_equivalent": "0", "succeeded": True}],
            },
        ],
    )
    measurement = aggregate_observation(payload)
    assert Decimal(measurement["attributed_cash"]) == Decimal("25")
    assert measurement["successful_tasks"] == "1"
    assert measurement["sample_size"] == "1"
    assert measurement["attempt_count"] == "3"
    assert Decimal(_measured(measurement)["metrics"]["K"]) == Decimal("25")


def test_zero_success_is_unbounded_or_insufficient_without_non_finite_json():
    paid = aggregate_observation(
        _sample(
            subscription_cash="5",
            tasks=[{"task_id": "failed", "attempts": [{"cash": "0", "succeeded": False}]}],
        )
    )
    unbounded = _measured(paid)
    assert unbounded["status"] == "unbounded"
    assert unbounded["metrics"]["K"] is None
    assert Decimal(unbounded["metrics"]["attributed_cash"]) == Decimal("5")
    empty = aggregate_observation(_sample(subscription_cash="0", tasks=[]))
    insufficient = _measured(empty)
    assert insufficient["status"] == "insufficient_data"
    assert insufficient["metrics"]["K"] is None
    assert Decimal(insufficient["metrics"]["attributed_cash"]) == Decimal("0")
    encoded = json.dumps({"unbounded": unbounded, "insufficient": insufficient}, allow_nan=False)
    assert "Infinity" not in encoded and "NaN" not in encoded


def test_repeat_import_does_not_duplicate_or_enter_the_catalog(tmp_path):
    store = Store(tmp_path / "measured.sqlite3")
    payload = _sample()
    public = aggregate_observation(payload)
    first, created = store.record_observation(payload, public, "batch-1")
    again, repeated = store.record_observation(payload, public, "batch-1")
    assert created is True
    assert repeated is False
    assert first["observation_id"] == again["observation_id"]
    assert "task-0" not in json.dumps(first)
    assert "retry of the same task" not in json.dumps(first)
    changed = _sample(subscription_cash="26")
    try:
        store.record_observation(changed, aggregate_observation(changed), "batch-1")
    except StoreError as exc:
        assert exc.code == "idempotency_conflict"
    else:
        raise AssertionError("changed body reused an idempotency key")
    try:
        store.record_observation(changed, aggregate_observation(changed), "batch-2")
    except StoreError as exc:
        assert exc.code == "scope_conflict"
    else:
        raise AssertionError("second measurement replaced the first")
    found = store.find_measurement(
        provider="example",
        channel="subscription",
        model="example-model",
        effort="high",
        plan="plus",
        feature_scope="code",
        currency="CNY",
        period_start="2026-09-01",
        period_end="2026-10-01",
        task_category="coding",
        acceptance="tests_passed",
    )
    assert Decimal(found["attributed_cash"]) == Decimal("25")
    assert found["observation_id"] == first["observation_id"]
    measured = _measured(found)
    assert measured["observation_id"] == first["observation_id"]
    assert measured["measurement"]["observation_id"] == first["observation_id"]
    assert store.catalog(None) == []
    store.close()


def test_missing_measurement_stays_missing_and_currency_is_not_converted():
    missing = _measured(None)
    assert missing["status"] == "missing_data"
    assert missing["metrics"] is None
    assert missing["metrics"] != {"K": "0"}
    foreign = _measured(aggregate_observation(_sample()), currency="USD")
    assert foreign["status"] == "invalid_input"
    assert "currency_conversion_refused" in foreign["assumptions"]
    assert foreign["metrics"] is None
    partial = _measured({"missing_fields": ["task_category", "acceptance", "period"]})
    assert partial["status"] == "missing_data"
    assert partial["missing_fields"] == ["task_category", "acceptance", "period"]
