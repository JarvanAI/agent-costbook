from __future__ import annotations

import re
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
_FORMULAS = {"M0": "m0-v1", "M4": "m4-v1"}


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
        result["metrics"] = dict(rates)
        result["units"] = {key: f"{record['currency']}_per_million_tokens" for key in rates}
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
