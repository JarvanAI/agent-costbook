from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from agent_costbook.estimates import RATE_KEYS
from agent_costbook.store import MAX_SAFE_INT, Store, StoreError

_VERSION = re.compile(r"[1-9][0-9]*")
_MONEY = {"B0", "C"}
_EXPORT_FIELDS = (
    ("P", "monthly_price"),
    ("m", "quota_multiplier"),
    ("N0", "baseline_tasks"),
    ("B0", "baseline_api_budget"),
    ("u", "utilization"),
    ("C", "cost_per_task"),
    ("w", "weight"),
    ("N", "measured_tasks"),
)


class ExportError(Exception):
    pass


def parse_data_version(text: str) -> int:
    if _VERSION.fullmatch(text) is None:
        raise ExportError("data_version is not a JSON-safe positive integer")
    value = int(text)
    if value > MAX_SAFE_INT:
        raise ExportError("data_version is not a JSON-safe positive integer")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _export_generation(snapshot) -> int | None:
    if "export_generation" not in snapshot.keys():
        return None
    value = snapshot["export_generation"]
    return None if value is None else int(value)


def _export_record(row: dict, evidence: dict[str, dict], generation: int | None = None) -> dict:
    stored = row.get("subscription") or {}
    missing: list[str] = []
    rates = row.get("rates") or {}
    if rates:
        exported_rates: dict | None = {}
        for key in RATE_KEYS:
            if key in rates:
                exported_rates[key] = {"amount": rates[key], "currency": row["currency"]}
            else:
                exported_rates[key] = None
                missing.append(key)
        exported_rates["tool_fee_per_call"] = None
        missing.append("tool_fee_per_call")
    elif stored:
        exported_rates = None
    else:
        exported_rates = {key: None for key in RATE_KEYS}
        exported_rates["tool_fee_per_call"] = None
        missing.extend(RATE_KEYS)
        missing.append("tool_fee_per_call")

    subscription = None
    if stored:
        subscription = {}
        for label, key in _EXPORT_FIELDS:
            value = stored.get(key)
            if value is None:
                subscription[label] = None
                missing.append(label)
                continue
            if label == "P":
                period = stored.get("price_period")
                subscription[label] = {
                    "amount": value,
                    "currency": row["currency"],
                    "period": period,
                }
                if not period:
                    missing.append("price_period")
            elif label in _MONEY:
                subscription[label] = {"amount": value, "currency": row["currency"]}
            else:
                subscription[label] = {"amount": value}

    sources = []
    for evidence_id in row.get("evidence_ids") or []:
        source = dict(evidence.get(evidence_id) or {"id": evidence_id, "kind": None})
        sources.append(source)
    sources.sort(key=lambda item: item.get("id") or "")
    scope = {"function": row["feature_scope"], "currency": row["currency"]}
    if stored.get("price_period"):
        scope["period"] = stored["price_period"]
    if row.get("window_start") or row.get("window_end"):
        scope["window"] = {
            "start": row.get("window_start") or None,
            "end": row.get("window_end") or None,
        }
    if generation == 2:
        scope["baseline_group"] = stored.get("baseline_group") or None
        scope["task_profile"] = stored.get("task_profile") or None
    exported = {
        "record_id": row["id"],
        "status": "ok",
        "provider": row["provider"],
        "channel": row["channel"],
        "model": row["model"],
        "effort": row.get("effort") or None,
        "plan": row["plan"],
        "scope": scope,
        "rates": exported_rates,
        "subscription": subscription,
        "missing_fields": missing,
        "sources": sources,
        "assumptions": list(stored.get("assumptions") or []),
        "research_id": row.get("research_id"),
    }
    if generation == 2 and row.get("record_status") == "conflict":
        exported["status"] = "conflict"
        exported["rates"] = None
        exported["subscription"] = None
        exported["missing_fields"] = []
    return exported


def build_document(store: Store, data_version: int | None) -> dict:
    snapshot = store.snapshot_for_export(data_version)
    if snapshot is None:
        raise ExportError("snapshot not found")
    rows = store.catalog(f"snap-{snapshot['revision']}")
    evidence = store.evidence_metadata(
        [evidence_id for row in rows for evidence_id in row.get("evidence_ids") or []]
    )
    generation = _export_generation(snapshot)
    records = [_export_record(row, evidence, generation) for row in rows]
    records.sort(key=lambda item: item["record_id"])
    document = {
        "kind": "agent-costbook.snapshot",
        "schema_version": 1,
        "publisher_id": store.publisher_id(),
        "data_version": snapshot["revision"],
        "snapshot_id": f"snap-{snapshot['revision']}",
        "published_at": snapshot["published_at"],
        "formula_version": snapshot["formula_version"],
        "freshness": None,
        "content_sha256": hashlib.sha256(_canonical(records)).hexdigest(),
        "records": records,
    }
    if generation == 2:
        document["conflicts"] = _conflict_summary(rows)
    return document


def _conflict_summary(rows: list[dict]) -> list[dict]:
    summary = []
    for row in rows:
        if row.get("record_status") != "conflict":
            continue
        summary.append(
            {
                "channel": row["channel"],
                "effort": row.get("effort") or None,
                "feature_scope": row["feature_scope"],
                "model": row["model"],
                "plan": row["plan"],
                "provider": row["provider"],
                "record_id": row["id"],
                "variants": list(row.get("conflict_variants") or []),
            }
        )
    summary.sort(key=lambda item: item["record_id"])
    return summary


def catalog_document(store: Store, data_version: int | None) -> dict:
    if data_version is None:
        return {
            "publisher_id": store.publisher_id(),
            "data_version": None,
            "snapshot_id": None,
            "published_at": None,
            "formula_version": None,
            "freshness": None,
            "content_sha256": None,
            "records": [],
        }
    document = build_document(store, data_version)
    kept = {
        key: document[key]
        for key in (
            "publisher_id",
            "data_version",
            "snapshot_id",
            "published_at",
            "formula_version",
            "freshness",
            "content_sha256",
            "records",
        )
    }
    if "conflicts" in document:
        kept["conflicts"] = document["conflicts"]
    return kept


def render(document: dict) -> bytes:
    return _canonical(document) + b"\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-costbook-export")
    parser.add_argument("--db", required=True)
    parser.add_argument("--data-version")
    args = parser.parse_args(argv)
    try:
        version = None if args.data_version is None else parse_data_version(args.data_version)
        path = Path(args.db)
        if not path.is_file():
            raise ExportError("database not found")
        try:
            store = Store(path, readonly=True)
        except StoreError as exc:
            if exc.code == "migration_required":
                raise ExportError(
                    "database schema needs migration before export; start the service once, then export"
                ) from exc
            if exc.code == "future_schema":
                raise ExportError("database schema is newer than this release") from exc
            raise ExportError("database not found") from exc
        try:
            payload = render(build_document(store, version))
        finally:
            store.close()
    except ExportError as exc:
        print(exc, file=sys.stderr)
        return 2
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
