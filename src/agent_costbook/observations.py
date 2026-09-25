from __future__ import annotations

from decimal import Decimal

from agent_costbook.estimates import money_text, parse_decimal

_IDENTITY = (
    "provider",
    "channel",
    "model",
    "effort",
    "plan",
    "feature_scope",
    "currency",
    "period_start",
    "period_end",
    "task_category",
    "acceptance",
)


def aggregate_observation(payload: dict) -> dict:
    """Sum one explicit import. Task text stays out of the returned measurement."""
    subscription = _money(payload.get("subscription_cash"), "subscription_cash")
    grouped: dict[str, list] = {}
    order: list[str] = []
    for task in payload.get("tasks") or []:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id")
        if task_id not in grouped:
            order.append(task_id)
            grouped[task_id] = []
        attempts = task.get("attempts") or []
        if not attempts:
            raise ValueError("attempts")
        grouped[task_id].extend(attempts)

    cash = subscription
    api_total = Decimal("0")
    api_known = True
    saw_api = False
    attempt_count = 0
    successes = 0
    for task_id in order:
        rows = grouped[task_id]
        attempt_count += len(rows)
        if any(_succeeded(row) for row in rows):
            successes += 1
        for row in rows:
            cash += _money(row.get("cash"), "cash")
            if row.get("api_equivalent") is None:
                api_known = False
                continue
            saw_api = True
            api_total += _money(row.get("api_equivalent"), "api_equivalent")

    measurement = {key: payload.get(key) for key in _IDENTITY}
    measurement.update(
        {
            "attributed_cash": money_text(cash),
            "successful_tasks": str(successes),
            "sample_size": str(len(order)),
            "attempt_count": str(attempt_count),
            "api_equivalent": money_text(api_total) if api_known and saw_api else None,
        }
    )
    return measurement


def _money(value: object, label: str) -> Decimal:
    number = parse_decimal(value)
    if number is None or number < 0:
        raise ValueError(label)
    return number


def _succeeded(row: dict) -> bool:
    value = row.get("succeeded")
    if not isinstance(value, bool):
        raise ValueError("succeeded")
    return value
