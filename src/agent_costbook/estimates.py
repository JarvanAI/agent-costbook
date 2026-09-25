from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

RATE_KEYS = (
    "uncached_input_per_million",
    "cache_read_per_million",
    "cache_write_per_million",
    "billed_output_per_million",
)
USAGE_TO_RATE = {
    "uncached_input": "uncached_input_per_million",
    "cache_read": "cache_read_per_million",
    "cache_write": "cache_write_per_million",
    "billed_output": "billed_output_per_million",
}
ALLOWED_USAGE = set(USAGE_TO_RATE) | {"reasoning"}
MILLION = Decimal("1000000")
_PLAIN_DECIMAL = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_FORMULAS = {
    "M0": "m0-v1",
    "M1": "m1-v1",
    "M2": "m2-v1",
    "M4": "m4-v1",
    "M6": "m6-v1",
}
_SUBSCRIPTION_METHODS = {"M1", "M2", "M6"}


def parse_decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or _PLAIN_DECIMAL.fullmatch(value) is None:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def money_text(number: Decimal) -> str:
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _result(candidate_id: str, method: str, record: dict | None) -> dict:
    freshness = None
    snapshot_id = None
    sources: list[str] = []
    if record is not None:
        snapshot_id = record.get("snapshot_id")
        sources = list(record.get("evidence_ids") or [])
        if record.get("published_at"):
            freshness = {"published_at": record["published_at"]}
    return {
        "candidate_id": candidate_id,
        "status": "ok",
        "metrics": None,
        "units": None,
        "method": method,
        "formula_version": None,
        "snapshot_id": snapshot_id,
        "sources": sources,
        "assumptions": [],
        "missing_fields": [],
        "freshness": freshness,
    }


def _invalid(result: dict, assumption: str | None = None) -> dict:
    result["status"] = "invalid_input"
    result["metrics"] = None
    result["units"] = None
    if assumption and assumption not in result["assumptions"]:
        result["assumptions"].append(assumption)
    if assumption == "invalid_usage":
        result["error_code"] = "invalid_usage"
    return result


def _effective_rates(record: dict, private_rates: dict | None, result: dict) -> dict | None:
    rates = dict(record.get("rates") or {})
    for key, value in rates.items():
        parsed = parse_decimal(value)
        if key not in RATE_KEYS or parsed is None or parsed < 0:
            return _invalid(result)
    if private_rates:
        result["assumptions"].append("request_private_rate_override")
        for key, value in private_rates.items():
            parsed = parse_decimal(value)
            if key not in RATE_KEYS or parsed is None or parsed < 0:
                return _invalid(result)
            rates[key] = value
    return rates


def _mark_missing(result: dict, fields: list[str]) -> dict:
    result["status"] = "missing_data"
    result["metrics"] = None
    result["units"] = None
    result["missing_fields"] = fields
    return result


def _read_amount(raw: object, *, low: Decimal, high: Decimal | None, greater_than: bool) -> tuple[Decimal | None, str]:
    if raw is None:
        return None, "missing"
    number = parse_decimal(raw)
    if number is None:
        return None, "invalid"
    if greater_than:
        if number <= low:
            return None, "invalid"
    elif number < low:
        return None, "invalid"
    if high is not None and number > high:
        return None, "invalid"
    return number, "ok"


def _cash_increment(result: dict, marginal_cash: str | None) -> str | None | object:
    if marginal_cash is None:
        result["missing_fields"].append("cash_increment")
        return None
    number = parse_decimal(marginal_cash)
    if number is None or number < 0:
        return _invalid(result)
    result["assumptions"].append("request_marginal_cash")
    return money_text(number)


def _finish_subscription(
    result: dict,
    *,
    currency: str,
    method: str,
    metrics: dict,
    cash: str | None,
    api_equivalent: Decimal | None,
) -> dict:
    metrics["cash_increment"] = cash
    if cash is None and "cash_increment" not in result["missing_fields"]:
        result["missing_fields"].append("cash_increment")
    metrics["quota_consumption"] = None
    if "quota_consumption" not in result["missing_fields"]:
        result["missing_fields"].append("quota_consumption")
    if api_equivalent is not None:
        metrics["api_equivalent"] = money_text(api_equivalent)
    elif "api_equivalent" not in result["missing_fields"]:
        result["missing_fields"].append("api_equivalent")
    result["metrics"] = metrics
    per = f"{currency}_per_quota_unit" if method == "M1" else f"{currency}_per_task"
    units = {"K": per, "amortization": per, "quota_consumption": "tasks"}
    if "N" in metrics:
        units["N"] = "tasks"
    if "api_equivalent" in metrics:
        units["api_equivalent"] = f"{currency}_per_task"
    if cash is not None:
        units["cash_increment"] = currency
    result["units"] = units
    return result


def _evaluate_subscription(
    result: dict,
    method: str,
    record: dict,
    marginal_cash: str | None,
    scenario_notes: list[str],
) -> dict:
    subscription = dict(record.get("subscription") or {})
    result["formula_version"] = _FORMULAS[method]
    for note in scenario_notes:
        if note not in result["assumptions"]:
            result["assumptions"].append(note)
    required = {
        "M1": ("monthly_price", "quota_multiplier"),
        "M2": ("monthly_price",),
        "M6": (
            "monthly_price",
            "quota_multiplier",
            "baseline_api_budget",
            "utilization",
            "cost_per_task",
        ),
    }[method]
    rules = {
        "monthly_price": (Decimal("0"), None, False),
        "quota_multiplier": (Decimal("0"), None, True),
        "baseline_tasks": (Decimal("0"), None, False),
        "measured_tasks": (Decimal("0"), None, False),
        "baseline_api_budget": (Decimal("0"), None, True),
        "utilization": (Decimal("0"), Decimal("1"), False),
        "cost_per_task": (Decimal("0"), None, True),
        "weight": (Decimal("0"), None, True),
    }
    values: dict[str, Decimal] = {}
    missing: list[str] = []
    for key, (low, high, greater) in rules.items():
        if key not in subscription or subscription.get(key) is None:
            if key in required:
                missing.append(key)
            continue
        number, state = _read_amount(subscription.get(key), low=low, high=high, greater_than=greater)
        if state == "invalid":
            return _invalid(result)
        if number is not None:
            values[key] = number
    period = subscription.get("price_period")
    if period is not None and period != "month":
        return _invalid(result, "price_period_not_monthly")
    if period is None and "price_period" not in missing:
        missing.append("price_period")
    if method == "M6" and "weight" not in values:
        values["weight"] = Decimal("1")
        result["assumptions"].append("default_weight_one")
    measured = values.get("measured_tasks")
    if method == "M2" and measured is None:
        for key in ("quota_multiplier", "baseline_tasks", "utilization"):
            if key not in values and key not in missing:
                missing.append(key)
    if missing:
        return _mark_missing(result, missing)

    cash = _cash_increment(result, marginal_cash)
    if result["status"] == "invalid_input":
        return result
    price = values["monthly_price"]
    api_equivalent = values.get("cost_per_task")

    if method == "M1":
        divisor = values["quota_multiplier"]
        cost = price / divisor
        if price == 0:
            result["assumptions"].append("explicit_zero_price")
        return _finish_subscription(
            result,
            currency=record["currency"],
            method=method,
            metrics={"K": money_text(cost), "amortization": money_text(cost)},
            cash=cash if isinstance(cash, str) else None,
            api_equivalent=api_equivalent,
        )
    if method == "M2" and measured is not None:
        task_count = measured
        result["assumptions"].append("measured_n_not_rescaled_by_utilization")
    elif method == "M2":
        task_count = values["quota_multiplier"] * values["baseline_tasks"] * values["utilization"]
    else:
        if values["utilization"] == 0:
            result["status"] = "no_capacity"
            return _finish_subscription(
                result,
                currency=record["currency"],
                method=method,
                metrics={"N": "0", "K": None, "amortization": None},
                cash=cash if isinstance(cash, str) else None,
                api_equivalent=api_equivalent,
            )
        task_count = (
            values["quota_multiplier"]
            * values["baseline_api_budget"]
            * values["utilization"]
            / values["cost_per_task"]
        )
    if task_count == 0:
        result["status"] = "no_capacity"
        return _finish_subscription(
            result,
            currency=record["currency"],
            method=method,
            metrics={"N": "0", "K": None, "amortization": None},
            cash=cash if isinstance(cash, str) else None,
            api_equivalent=api_equivalent,
        )
    if method == "M6":
        cost = (
            price
            * values["cost_per_task"]
            / (
                values["quota_multiplier"]
                * values["baseline_api_budget"]
                * values["utilization"]
                * values["weight"]
            )
        )
    else:
        cost = price / task_count
    if price == 0:
        result["assumptions"].append("explicit_zero_price")
    return _finish_subscription(
        result,
        currency=record["currency"],
        method=method,
        metrics={
            "N": money_text(task_count),
            "K": money_text(cost),
            "amortization": money_text(cost),
        },
        cash=cash if isinstance(cash, str) else None,
        api_equivalent=api_equivalent,
    )


def _same_borrow_scope(record: dict, reference: dict) -> bool:
    if not record.get("currency") or record.get("currency") != reference.get("currency"):
        return False
    if record.get("feature_scope") != reference.get("feature_scope"):
        return False
    left = record.get("subscription") or {}
    right = reference.get("subscription") or {}
    if (left.get("price_period") or "") != (right.get("price_period") or ""):
        return False
    profile = left.get("task_profile") or ""
    return bool(profile) and profile == (right.get("task_profile") or "")


def apply_scenario(
    record: dict | None,
    reference_record: dict | None,
    scenario: dict | None,
) -> tuple[dict | None, list[str]]:
    if record is None:
        return None, []
    subscription = dict(record.get("subscription") or {})
    if (
        not scenario
        or reference_record is None
        or reference_record.get("id") == record.get("id")
        or not _same_borrow_scope(record, reference_record)
    ):
        return subscription, []
    reference = reference_record.get("subscription") or {}
    notes: list[str] = []
    if scenario.get("equal_baseline_budget") and not subscription.get("baseline_api_budget"):
        borrowed = reference.get("baseline_api_budget")
        if borrowed:
            subscription["baseline_api_budget"] = borrowed
            notes.append("assumed_equal_baseline_budget")
    if (
        scenario.get("equal_baseline_tasks")
        and not subscription.get("baseline_tasks")
        and not subscription.get("measured_tasks")
    ):
        borrowed = reference.get("baseline_tasks")
        if borrowed:
            subscription["baseline_tasks"] = borrowed
            notes.append("assumed_equal_baseline_tasks")
    return subscription, notes


def _comparison_match(method: str, left: dict, right: dict, allow_cross_provider: bool) -> bool:
    if not left.get("currency") or left.get("currency") != right.get("currency"):
        return False
    if left.get("feature_scope") != right.get("feature_scope"):
        return False
    if (left.get("window_start"), left.get("window_end")) != (
        right.get("window_start"),
        right.get("window_end"),
    ):
        return False
    if (left.get("price_period") or "") != (right.get("price_period") or ""):
        return False
    if method == "M1":
        group = left.get("baseline_group") or ""
        return (
            bool(group)
            and group == (right.get("baseline_group") or "")
            and left.get("provider") == right.get("provider")
        )
    profile = left.get("task_profile") or ""
    if not profile or profile != (right.get("task_profile") or ""):
        return False
    if method == "M6" and left.get("has_own_budget") and right.get("has_own_budget"):
        return True
    if method == "M2" and left.get("has_own_tasks") and right.get("has_own_tasks"):
        return True
    return bool(allow_cross_provider)


def apply_reference_comparison(
    method: str,
    results: list[dict],
    contexts: list[dict],
    *,
    reference_candidate_id: str,
    allow_cross_provider: bool = False,
) -> list[dict]:
    copied: list[dict] = []
    for result in results:
        item = dict(result)
        item["assumptions"] = list(item.get("assumptions") or [])
        if isinstance(item.get("metrics"), dict):
            item["metrics"] = dict(item["metrics"])
        if isinstance(item.get("units"), dict):
            item["units"] = dict(item["units"])
        copied.append(item)
    reference_index = next(
        (
            index
            for index, item in enumerate(copied)
            if item["candidate_id"] == reference_candidate_id
        ),
        None,
    )
    if reference_index is None or len(copied) != len(contexts):
        return copied
    reference = copied[reference_index]
    reference_context = contexts[reference_index]
    rankable = reference["status"] == "ok" and _numeric_k(reference) is not None
    reference_k = _numeric_k(reference)
    for index, (item, context) in enumerate(zip(copied, contexts)):
        if index == reference_index:
            continue
        matched = _comparison_match(method, context, reference_context, allow_cross_provider)
        if item["status"] != "ok" or not matched or _numeric_k(item) is None:
            rankable = False
        if not matched:
            if "not_comparable" not in item["assumptions"]:
                item["assumptions"].append("not_comparable")
            continue
        if item["status"] != "ok" or not isinstance(item.get("metrics"), dict):
            continue
        if reference_k is None or reference_k <= 0:
            if "reference_ratio_unavailable" not in item["assumptions"]:
                item["assumptions"].append("reference_ratio_unavailable")
            continue
        candidate_k = _numeric_k(item)
        if candidate_k is None:
            continue
        item["metrics"]["cost_ratio"] = money_text(candidate_k / reference_k)
        if isinstance(item.get("units"), dict):
            item["units"]["cost_ratio"] = "ratio"
    if rankable:
        order = sorted(
            range(len(copied)),
            key=lambda index: (_numeric_k(copied[index]) or Decimal("0"), copied[index]["candidate_id"]),
        )
        for rank, index in enumerate(order, start=1):
            copied[index]["rank"] = rank
    return copied


def _numeric_k(result: dict) -> Decimal | None:
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        return None
    value = metrics.get("K")
    if not isinstance(value, str):
        return None
    return parse_decimal(value)


def evaluate_candidate(
    *,
    candidate_id: str,
    method: str,
    usage: dict | None,
    extra_cost: str | None,
    currency: str | None,
    private_rates: dict | None,
    record: dict | None,
    conflict: bool,
    marginal_cash: str | None = None,
    subscription_override: dict | None = None,
    scenario_notes: list[str] | None = None,
    private_subscription: dict | None = None,
) -> dict:
    result = _result(candidate_id, method, record)
    if method not in _FORMULAS:
        result["status"] = "unsupported_method"
        result["snapshot_id"] = None
        result["sources"] = []
        result["freshness"] = None
        return result
    if conflict:
        result["status"] = "conflict"
        return result
    if record is None:
        result["status"] = "missing_data"
        result["missing_fields"] = ["record"]
        return result
    if currency and currency != record.get("currency"):
        return _invalid(result, "currency_conversion_refused")
    if method in _SUBSCRIPTION_METHODS:
        working = dict(record)
        if subscription_override is not None:
            working["subscription"] = {
                **dict(record.get("subscription") or {}),
                **subscription_override,
            }
        notes = list(scenario_notes or [])
        private_price = (private_subscription or {}).get("monthly_price")
        if private_price is not None:
            working["subscription"] = {
                **dict(working.get("subscription") or {}),
                "monthly_price": private_price,
            }
            notes.append("request_private_subscription")
        evaluated = _evaluate_subscription(
            result,
            method,
            working,
            marginal_cash,
            notes,
        )
        if private_price is not None:
            evaluated["subscription_provenance"] = {"monthly_price": "request_override"}
        return evaluated

    rates = _effective_rates(record, private_rates, result)
    if result["status"] == "invalid_input":
        return result
    assert rates is not None
    result["formula_version"] = _FORMULAS[method]
    result["rate_provenance"] = {
        key: "request_override" if private_rates and key in private_rates else "public"
        for key in rates
    }
    if method == "M0":
        metrics = dict(rates)
        units = {key: f"{record['currency']}_per_million_tokens" for key in rates}
        subscription = dict(record.get("subscription") or {})
        private_price = (private_subscription or {}).get("monthly_price")
        if private_price is not None:
            subscription["monthly_price"] = private_price
            result["assumptions"].append("request_private_subscription")
            result["subscription_provenance"] = {"monthly_price": "request_override"}
        if subscription:
            for key in (
                "monthly_price",
                "quota_multiplier",
                "baseline_tasks",
                "measured_tasks",
                "baseline_api_budget",
                "utilization",
                "cost_per_task",
                "weight",
            ):
                value = subscription.get(key)
                metrics[key] = value
                if value is None:
                    result["missing_fields"].append(key)
                else:
                    units[key] = (
                        record["currency"]
                        if key in {"monthly_price", "baseline_api_budget", "cost_per_task"}
                        else "count"
                    )
        result["metrics"] = metrics
        result["units"] = units
        return result

    supplied = usage or {}
    if not supplied:
        result["status"] = "missing_data"
        result["missing_fields"] = ["usage"]
        result["metrics"] = None
        return result
    parsed_usage: dict[str, Decimal] = {}
    if any(key not in ALLOWED_USAGE for key in supplied):
        return _invalid(result, "invalid_usage")
    for key, value in supplied.items():
        number = parse_decimal(value)
        if number is None or number < 0:
            return _invalid(result, "invalid_usage")
        parsed_usage[key] = number
    if "reasoning" in parsed_usage:
        billed = parsed_usage.get("billed_output")
        if billed is None or parsed_usage["reasoning"] > billed:
            return _invalid(result, "invalid_usage")
        result["assumptions"].append("reasoning_tokens_included_in_billed_output")

    if extra_cost is None:
        result["status"] = "missing_data"
        result["missing_fields"] = ["extra_cost"]
        result["metrics"] = None
        return result
    extra = parse_decimal(extra_cost)
    if extra is None or extra < 0:
        return _invalid(result, "invalid_usage")

    missing = [
        USAGE_TO_RATE[key]
        for key, amount in parsed_usage.items()
        if key in USAGE_TO_RATE and amount > 0 and USAGE_TO_RATE[key] not in rates
    ]
    if missing:
        result["status"] = "missing_data"
        result["missing_fields"] = missing
        result["metrics"] = None
        return result

    total = extra
    for key, amount in parsed_usage.items():
        if key == "reasoning":
            continue
        rate_key = USAGE_TO_RATE[key]
        if amount == 0 and rate_key not in rates:
            continue
        total += amount * parse_decimal(rates[rate_key]) / MILLION
    if not total.is_finite():
        return _invalid(result, "invalid_usage")
    result["metrics"] = {"cost": money_text(total), "currency": record["currency"]}
    result["units"] = {"cost": record["currency"]}
    return result


def freshness_view(
    published_at: str | None,
    *,
    now: str | None = None,
    max_age_seconds: int | None = None,
) -> dict:
    view = {"published_at": published_at, "stale": None}
    if not published_at or now is None or max_age_seconds is None:
        return view
    try:
        published = datetime.fromisoformat(published_at)
        current = datetime.fromisoformat(now)
    except ValueError:
        return view
    if published.tzinfo is None or current.tzinfo is None:
        return view
    view["stale"] = (current - published).total_seconds() > max_age_seconds
    return view


def run_estimate(
    *,
    method: str,
    usage: dict | None,
    extra_cost: str | None,
    currency: str | None,
    scenario: dict | None,
    reference_candidate_id: str | None,
    candidates: list[dict],
    selections: list,
    compare: bool = True,
) -> list[dict]:
    paired = list(zip(candidates, selections))
    reference_record = None
    if reference_candidate_id:
        for candidate, selection in paired:
            if candidate["candidate_id"] == reference_candidate_id:
                reference_record = selection.record
    results = []
    contexts = []
    for candidate, selection in paired:
        subscription, notes = apply_scenario(
            selection.record,
            reference_record,
            scenario,
        )
        result = evaluate_candidate(
            candidate_id=candidate["candidate_id"],
            method=method,
            usage=usage,
            extra_cost=extra_cost,
            currency=currency,
            private_rates=candidate.get("private_rates") or None,
            record=selection.record,
            conflict=selection.conflict,
            marginal_cash=candidate.get("marginal_cash"),
            subscription_override=subscription,
            scenario_notes=notes,
            private_subscription=candidate.get("private_subscription") or None,
        )
        result["record_snapshot_id"] = (
            selection.record.get("snapshot_id") if selection.record else None
        )
        record = selection.record or {}
        stored = record.get("subscription") or {}
        contexts.append(
            {
                "candidate_id": candidate["candidate_id"],
                "provider": record.get("provider", candidate["provider"]),
                "feature_scope": record.get("feature_scope", candidate["feature_scope"]),
                "window_start": record.get("window_start") or "",
                "window_end": record.get("window_end") or "",
                "currency": record.get("currency"),
                "model": record.get("model", candidate["model"]),
                "effort": record.get("effort", candidate["effort"]),
                "task_profile": stored.get("task_profile") or "",
                "price_period": stored.get("price_period") or "",
                "baseline_group": stored.get("baseline_group") or "",
                "has_own_budget": bool(stored.get("baseline_api_budget")),
                "has_own_tasks": bool(stored.get("baseline_tasks") or stored.get("measured_tasks")),
            }
        )
        results.append(result)
    if reference_candidate_id and compare:
        allow_cross_provider = bool(
            scenario
            and (scenario.get("equal_baseline_budget") or scenario.get("equal_baseline_tasks"))
        )
        results = apply_reference_comparison(
            method,
            results,
            contexts,
            reference_candidate_id=reference_candidate_id,
            allow_cross_provider=allow_cross_provider,
        )
    return results
