#!/usr/bin/env python3
"""Compile a reviewed public catalog SQLite file from an approved JSON source.

The script does not fetch prices. UUIDs come from Store only when a new database
is created. An update copies the previous database so publisher and row history
stay put. A conditional OpenRouter override is refused instead of stored as one
flat rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from agent_costbook.capabilities import AgentCapabilityIn, ModelEffortIn
from agent_costbook.models import ContributionIn
from agent_costbook.public_data import PublicDataError, validate_public_artifact
from agent_costbook.store import Store, StoreError

_MILLION = Decimal(1_000_000)
_RATE_FIELDS = (
    ("prompt", "uncached_input_per_million"),
    ("completion", "billed_output_per_million"),
    ("input_cache_read", "cache_read_per_million"),
    ("input_cache_write", "cache_write_per_million"),
)
_RATE_KEYS = tuple(item[1] for item in _RATE_FIELDS)
_AGENT_FIELDS = (
    "agent_id",
    "domain",
    "strengths",
    "unsuitable",
    "can_edit_files",
    "can_use_tools",
    "input_output_shape",
    "source",
    "source_ref",
    "as_of",
)
_EFFORT_FIELDS = (
    "provider",
    "model",
    "effort",
    "suitable",
    "unsuitable",
    "context_length",
    "benchmarks",
    "default_for_agents",
    "source",
    "source_ref",
    "as_of",
)


def decimal_per_million(raw: str) -> str:
    value = Decimal(raw) * _MILLION
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def rates_from_raw(raw: dict) -> dict:
    # A second context band or cache TTL cannot be selected by the current record key.
    if raw.get("overrides"):
        raise PublicDataError(
            "conditional_price",
            "a pricing condition cannot be stored as a flat rate",
        )
    rates = {}
    for raw_key, rate_key in _RATE_FIELDS:
        value = raw.get(raw_key)
        rates[rate_key] = None if value in (None, "") else decimal_per_million(str(value))
    if raw.get("input_cache_write_1h") not in (None, ""):
        rates["cache_write_per_million"] = None
    return rates


def compile_public_catalog(source: dict, output, manifest_path, update_from=None) -> dict:
    target = Path(output)
    manifest_target = Path(manifest_path)
    if target.exists() or manifest_target.exists():
        raise PublicDataError("output_exists", "refusing to overwrite an existing public artifact")
    if update_from is not None and Path(update_from).resolve() == target.resolve():
        raise PublicDataError("output_exists", "refusing to overwrite an existing public artifact")
    _preflight_prices(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial_manifest = manifest_target.with_name(manifest_target.name + ".partial")
    _remove_partial(partial, partial_manifest)
    try:
        if update_from is None:
            store = Store(partial)
        else:
            previous = Path(update_from)
            if not previous.is_file():
                raise PublicDataError("artifact_unreadable", "previous public database is not a file")
            _require_delete_file(previous)
            shutil.copy2(previous, partial)
            store = Store(partial)
        try:
            _apply(store, source)
        finally:
            store.close()
        _seal(partial)
        _write_manifest(partial, partial_manifest, source)
        checked = validate_public_artifact(partial, partial_manifest)
        os.replace(partial, target)
        os.replace(partial_manifest, manifest_target)
        return checked
    except Exception:
        _remove_partial(partial, partial_manifest)
        raise


def _preflight_prices(source: dict) -> None:
    for price in source.get("prices") or []:
        if price.get("effort"):
            raise PublicDataError(
                "conditional_price",
                "effort-specific prices are not a flat public rate",
            )
        rates_from_raw(price.get("raw_pricing") or {})


def _apply(store: Store, source: dict) -> None:
    try:
        _publish_prices(store, source)
        _save_capabilities(store, source)
    except (StoreError, ValueError):
        raise PublicDataError("artifact_unreadable", "the public catalog could not be written") from None


def _publish_prices(store: Store, source: dict) -> None:
    current = {_identity(row): row for row in store.catalog(None)}
    records = []
    for price in source["prices"]:
        rates = rates_from_raw(price["raw_pricing"])
        record = _record(price, rates)
        existing = current.get(_identity(record))
        if existing is None:
            records.append(record)
            continue
        if _same_rates(existing["rates"], rates):
            continue
        record["base_snapshot_id"] = existing["snapshot_id"]
        records.append(record)
    if not records:
        return
    payload = {
        "research": source["research"],
        "evidence": source["evidence"],
        "records": records,
    }
    checked = ContributionIn.model_validate(payload).model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(checked, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    created, _inserted = store.create_contribution(checked, f"public-{digest}")
    store.publish(created["contribution_id"])


def _record(price: dict, rates: dict) -> dict:
    return {
        "provider": price["provider"],
        "channel": price.get("channel") or "openrouter",
        "model": price["model"],
        "effort": "",
        "plan": price.get("plan") or "payg",
        "feature_scope": price.get("feature_scope") or "text",
        "currency": price.get("currency") or "USD",
        "rates": rates,
        "evidence_indexes": [int(price.get("evidence_index") or 0)],
    }


def _identity(row: dict) -> tuple:
    return (
        row["provider"],
        row.get("channel") or "openrouter",
        row["model"],
        row.get("effort") or "",
        row.get("plan") or "payg",
        row.get("feature_scope") or "text",
        row.get("window_start") or "",
        row.get("window_end") or "",
        row.get("currency") or "USD",
    )


def _same_rates(stored: dict, proposed: dict) -> bool:
    for key in _RATE_KEYS:
        left = stored.get(key)
        right = proposed.get(key)
        if left is None and right is None:
            continue
        if left is None or right is None or Decimal(left) != Decimal(right):
            return False
    return True


def _save_capabilities(store: Store, source: dict) -> None:
    for agent in source.get("agents") or []:
        current = store.agent_capability(agent["agent_id"])
        expected = 0 if current is None else int(current["row_version"])
        if current is not None and _same_fields(current, agent, _AGENT_FIELDS):
            continue
        payload = dict(agent)
        payload["expected_version"] = expected
        store.save_agent_capability(AgentCapabilityIn.model_validate(payload).model_dump(mode="json"))
    for effort in source.get("model_efforts") or []:
        current = store.model_effort(effort["provider"], effort["model"], effort.get("effort"))
        expected = 0 if current is None else int(current["row_version"])
        if current is not None and _same_fields(current, effort, _EFFORT_FIELDS):
            continue
        payload = dict(effort)
        payload["expected_version"] = expected
        store.save_model_effort(ModelEffortIn.model_validate(payload).model_dump(mode="json"))


def _same_fields(stored: dict, submitted: dict, fields: tuple[str, ...]) -> bool:
    for field in fields:
        if stored.get(field) != submitted.get(field):
            return False
    return True


def _require_delete_file(path: Path) -> None:
    if Path(str(path) + "-wal").exists() or Path(str(path) + "-shm").exists():
        raise PublicDataError("sidecar_dependency", "previous public database has a WAL or SHM sidecar")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    if str(mode).lower() != "delete":
        raise PublicDataError("journal_invalid", "previous public database must use DELETE journaling")


def _seal(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        connection.commit()
    finally:
        connection.close()
    if str(mode).lower() != "delete":
        raise PublicDataError("journal_invalid", "public database must use DELETE journaling")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _write_manifest(database: Path, manifest_path: Path, source: dict) -> dict:
    store = Store(database, readonly=True)
    try:
        current = store.current_capabilities()
        manifest = {
            "kind": "agent-costbook.publication",
            "schema_version": 1,
            "reviewed": True,
            "reviewed_at": source["reviewed_at"],
            "publisher_id": store.publisher_id(),
            "database_sha256": _sha256(database),
            "data_version": store.revision_of(None),
            "coverage": {
                "prices": len(store.catalog(None)),
                "agents": len(current["agents"]),
                "model_efforts": len(current["model_efforts"]),
            },
            "sources": source["sources"],
            "exclusions": source["exclusions"],
        }
    finally:
        store.close()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_partial(database: Path, manifest_path: Path) -> None:
    database.unlink(missing_ok=True)
    manifest_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build-public-catalog")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--update-from")
    args = parser.parse_args(argv)
    try:
        source = json.loads(Path(args.source).read_text(encoding="utf-8"))
        manifest = compile_public_catalog(
            source,
            args.output,
            args.manifest,
            update_from=args.update_from,
        )
    except PublicDataError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 2
    print(f"{manifest['publisher_id']} {manifest['database_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
