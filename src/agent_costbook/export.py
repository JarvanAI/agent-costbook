from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from agent_costbook.estimates import RATE_KEYS
from agent_costbook.store import MAX_SAFE_INT, Store

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


def _export_record(row: dict, evidence: dict[str, dict]) -> dict:
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
    elif stored:
        exported_rates = None
    else:
        exported_rates = {key: None for key in RATE_KEYS}
        missing.extend(RATE_KEYS)

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
    return {
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


def build_document(store: Store, data_version: int | None) -> dict:
    snapshot = store.snapshot_for_export(data_version)
    if snapshot is None:
        raise ExportError("snapshot not found")
    rows = store.catalog(f"snap-{snapshot['revision']}")
    evidence = store.evidence_metadata(
        [evidence_id for row in rows for evidence_id in row.get("evidence_ids") or []]
    )
    records = [_export_record(row, evidence) for row in rows]
    records.sort(key=lambda item: item["record_id"])
    return {
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
        store = Store(path)
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
