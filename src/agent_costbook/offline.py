from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agent_costbook.capabilities import unavailable_capability
from agent_costbook.estimates import freshness_view, run_estimate
from agent_costbook.export import _canonical
from agent_costbook.models import EstimateIn
from agent_costbook.store import MAX_SAFE_INT, SUPPORTED_FORMULA_SETS
_LABELS = (
    ("P", "monthly_price"),
    ("m", "quota_multiplier"),
    ("N0", "baseline_tasks"),
    ("B0", "baseline_api_budget"),
    ("u", "utilization"),
    ("C", "cost_per_task"),
    ("w", "weight"),
    ("N", "measured_tasks"),
)


class SnapshotError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _Selection:
    record: dict | None
    conflict: bool


def load_snapshot(raw: bytes, *, expected_publisher: str | None) -> dict:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotError("json") from exc
    if not isinstance(document, dict):
        raise SnapshotError("json")
    if document.get("kind") != "agent-costbook.snapshot":
        raise SnapshotError("kind")
    if document.get("schema_version") != 1:
        raise SnapshotError("schema")
    publisher = document.get("publisher_id")
    if not isinstance(publisher, str) or not publisher:
        raise SnapshotError("publisher")
    if expected_publisher is not None and publisher != expected_publisher:
        raise SnapshotError("publisher")
    version = document.get("data_version")
    if type(version) is not int or isinstance(version, bool) or version < 1 or version > MAX_SAFE_INT:
        raise SnapshotError("data_version")
    if document.get("snapshot_id") != f"snap-{version}":
        raise SnapshotError("snapshot_id")
    records = document.get("records")
    if not isinstance(records, list):
        raise SnapshotError("records")
    digest = hashlib.sha256(_canonical(records)).hexdigest()
    if document.get("content_sha256") != digest:
        raise SnapshotError("content_sha256")
    return document


def _amount_rejected(value: dict, scope_currency: object, *, money: bool) -> bool:
    amount_currency = value.get("currency")
    if amount_currency is not None and amount_currency != scope_currency:
        return True
    unit = value.get("unit")
    if unit is None:
        return False
    if money:
        return unit != "per_million_tokens"
    return unit not in {"count", "ratio"}


def _internal_record(row: dict, document: dict) -> dict:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else {}
    scope_currency = scope.get("currency")
    rates_raw = row.get("rates") if isinstance(row.get("rates"), dict) else {}
    rates = {}
    quote_rejected = False
    for key, value in rates_raw.items():
        if not isinstance(value, dict) or value.get("amount") is None:
            continue
        if _amount_rejected(value, scope_currency, money=True):
            quote_rejected = True
            continue
        rates[key] = value["amount"]
    subscription: dict = {}
    raw_subscription = row.get("subscription") if isinstance(row.get("subscription"), dict) else {}
    for label, key in _LABELS:
        value = raw_subscription.get(label)
        if not isinstance(value, dict) or value.get("amount") is None:
            continue
        if _amount_rejected(value, scope_currency, money=label in {"P", "B0", "C"}):
            quote_rejected = True
            continue
        subscription[key] = value["amount"]
        if label == "P" and value.get("period"):
            subscription["price_period"] = value["period"]
    if scope.get("period") and "price_period" not in subscription:
        subscription["price_period"] = scope["period"]
    if scope.get("task_profile"):
        subscription["task_profile"] = scope["task_profile"]
    if scope.get("baseline_group"):
        subscription["baseline_group"] = scope["baseline_group"]
    window = scope.get("window") if isinstance(scope.get("window"), dict) else {}
    sources = row.get("sources") if isinstance(row.get("sources"), list) else []
    evidence_ids = [
        item.get("id")
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    retrieved = _earliest_retrieved(sources)
    record = {
        "id": row.get("record_id"),
        "provider": row.get("provider"),
        "channel": row.get("channel"),
        "model": row.get("model"),
        "effort": row.get("effort") or "",
        "plan": row.get("plan"),
        "feature_scope": scope.get("function"),
        "window_start": window.get("start") or "",
        "window_end": window.get("end") or "",
        "currency": scope_currency,
        "rates": rates,
        "evidence_ids": evidence_ids,
        "published_at": document.get("published_at"),
        "source_retrieved_at": retrieved,
        "snapshot_id": None,
        "quote_rejected": quote_rejected,
    }
    if row.get("status") == "conflict":
        record["record_status"] = "conflict"
    if subscription:
        record["subscription"] = subscription
    return record


def _earliest_retrieved(sources: list) -> str | None:
    from datetime import datetime

    if not sources:
        return None
    parsed = []
    for item in sources:
        if not isinstance(item, dict):
            return None
        value = item.get("retrieved_at")
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed.append((datetime.fromisoformat(value), value))
        except ValueError:
            return None
    parsed.sort()
    return parsed[0][1]


def _ready(records: list) -> bool:
    return all(
        isinstance(row, dict)
        and isinstance(row.get("scope"), dict)
        and "task_profile" in row["scope"]
        and "baseline_group" in row["scope"]
        for row in records
    )


def _identity(record: dict) -> tuple:
    return (
        record.get("provider"),
        record.get("channel"),
        record.get("model"),
        record.get("effort") or "",
        record.get("plan"),
        record.get("feature_scope"),
        record.get("window_start") or "",
        record.get("window_end") or "",
        record.get("currency"),
    )


def _select(records: list[dict], candidate, currency: str | None) -> _Selection:
    matched = []
    for record in records:
        if record.get("provider") != candidate.provider or record.get("channel") != candidate.channel:
            continue
        if record.get("model") != candidate.model or (record.get("effort") or "") != candidate.effort:
            continue
        if record.get("plan") != candidate.plan or record.get("feature_scope") != candidate.feature_scope:
            continue
        if currency is not None and record.get("currency") != currency:
            continue
        if candidate.window_start is not None or candidate.window_end is not None:
            if record.get("window_start") != (candidate.window_start or ""):
                continue
            if record.get("window_end") != (candidate.window_end or ""):
                continue
        matched.append(record)
    grouped: dict[tuple, dict] = {}
    for record in matched:
        key = _identity(record)
        if key in grouped:
            return _Selection(None, True)
        grouped[key] = record
    if len(grouped) > 1:
        return _Selection(None, True)
    if not grouped:
        return _Selection(None, False)
    return _Selection(next(iter(grouped.values())), False)


def estimate_snapshot(
    document: dict,
    body: EstimateIn,
    *,
    now: str | None = None,
    max_age_seconds: int | None = None,
) -> dict:
    formula = document.get("formula_version")
    if formula not in SUPPORTED_FORMULA_SETS:
        raise SnapshotError("unsupported_formula")
    if body.snapshot_id is not None and body.snapshot_id != document.get("snapshot_id"):
        raise SnapshotError("snapshot_id")
    rows = document["records"]
    ready = _ready(rows)
    internal = [_internal_record(row, document) for row in rows if isinstance(row, dict)]
    compare = not (body.reference_candidate_id and not ready)
    if body.reference_candidate_id and not ready:
        comparison = "unavailable"
    elif body.reference_candidate_id:
        comparison = "available"
    else:
        comparison = "not_requested"
    selections = [_select(internal, candidate, body.currency) for candidate in body.candidates]
    results = run_estimate(
        method=body.method,
        usage=body.usage,
        extra_cost=body.extra_cost,
        currency=body.currency,
        scenario=body.scenario.model_dump() if body.scenario is not None else None,
        reference_candidate_id=body.reference_candidate_id,
        candidates=[_candidate(candidate) for candidate in body.candidates],
        selections=selections,
        compare=compare,
        formula_set=formula,
    )
    for result in results:
        result["publisher_id"] = document["publisher_id"]
        result["data_version"] = document["data_version"]
        result["snapshot_id"] = document["snapshot_id"]
        result["capabilities"] = unavailable_capability()
    payload = {
        "kind": document["kind"],
        "schema_version": document["schema_version"],
        "publisher_id": document["publisher_id"],
        "data_version": document["data_version"],
        "snapshot_id": document["snapshot_id"],
        "published_at": document.get("published_at"),
        "formula_version": formula,
        "freshness": None,
        "content_sha256": document["content_sha256"],
        "comparison": comparison,
        "results": results,
        "read_view": {
            "records": [
                {
                    "candidate_id": result["candidate_id"],
                    "freshness": freshness_view(
                        (result.get("freshness") or {}).get("retrieved_at"),
                        now=now,
                        max_age_seconds=max_age_seconds,
                    ),
                }
                for result in results
            ],
            "comparison_context": "available" if ready else "unavailable",
        },
    }
    if "conflicts" in document:
        payload["conflicts"] = document["conflicts"]
    return payload


def _candidate(candidate) -> dict:
    private = None
    if candidate.private_rates is not None:
        private = candidate.private_rates.model_dump(exclude_none=True)
    private_subscription = None
    if candidate.private_subscription is not None:
        private_subscription = candidate.private_subscription.model_dump(exclude_none=True)
    return {
        "candidate_id": candidate.candidate_id,
        "provider": candidate.provider,
        "channel": candidate.channel,
        "model": candidate.model,
        "effort": candidate.effort,
        "plan": candidate.plan,
        "feature_scope": candidate.feature_scope,
        "window_start": candidate.window_start,
        "window_end": candidate.window_end,
        "private_rates": private or None,
        "private_subscription": private_subscription,
        "marginal_cash": candidate.marginal_cash,
    }


def _emit(document: dict) -> None:
    sys.stdout.buffer.write(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ac")
    commands = parser.add_subparsers(dest="command", required=True)
    estimate = commands.add_parser("estimate")
    estimate.add_argument("--snapshot", required=True)
    estimate.add_argument("--request", required=True)
    estimate.add_argument("--publisher")
    estimate.add_argument("--now")
    estimate.add_argument("--max-age-seconds", type=int)
    collect = commands.add_parser("collect")
    collect.add_argument("--db", required=True)
    collect.add_argument("--once", action="store_true")
    collect.add_argument("--max-runtime", type=float)
    collect.add_argument("--now")
    args = parser.parse_args(argv)
    if args.command == "collect":
        return _collect(args.db, args.now, args.max_runtime)
    return _estimate(args)


def _estimate(args) -> int:
    snapshot = Path(args.snapshot)
    request = Path(args.request)
    if not snapshot.is_file() or not request.is_file():
        print("snapshot", file=sys.stderr)
        return 2
    try:
        document = load_snapshot(snapshot.read_bytes(), expected_publisher=args.publisher)
        body = EstimateIn.model_validate_json(request.read_text(encoding="utf-8"))
        payload = estimate_snapshot(
            document,
            body,
            now=args.now,
            max_age_seconds=args.max_age_seconds,
        )
    except SnapshotError as exc:
        if exc.code == "unsupported_formula":
            _emit(
                {
                    "formula_version": json.loads(snapshot.read_text(encoding="utf-8")).get(
                        "formula_version"
                    ),
                    "status": "unsupported_formula",
                }
            )
            return 3
        print(exc.code, file=sys.stderr)
        return 2
    except ValidationError:
        print("invalid_request", file=sys.stderr)
        return 2
    _emit(payload)
    return 0


def _collect(db: str, now: str | None, max_runtime: float | None) -> int:
    import signal
    import time
    from datetime import datetime, timezone

    from agent_costbook.store import Store, StoreError
    from agent_costbook.worker import Worker, collect_loop, production_fetch

    class _Wall:
        def iso(self) -> str:
            return now or datetime.now(timezone.utc).isoformat()

        def monotonic(self) -> float:
            return time.monotonic()

        def sleep(self, seconds: float) -> None:
            time.sleep(seconds)

    try:
        store = Store(db)
    except StoreError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    stopped = False

    def _stop(signum, frame):
        nonlocal stopped
        stopped = True

    try:
        if max_runtime is None:
            stamp = now or datetime.now(timezone.utc).isoformat()
            result = Worker(store, fetch=production_fetch).tick(stamp)
            print(result["status"])
            return 0 if result["status"] != "failed" else 1
        signal.signal(signal.SIGTERM, _stop)
        results = collect_loop(
            store,
            production_fetch,
            clock=_Wall(),
            max_runtime=max_runtime,
            stop=lambda: stopped,
        )
    finally:
        store.close()
    if not results:
        print("stopped")
        return 0
    print(results[-1]["status"])
    return 0 if results[-1]["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
